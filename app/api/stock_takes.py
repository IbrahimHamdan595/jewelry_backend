"""Physical stock-take workflow (audit phase B2).

DESIGN PRINCIPLE
----------------
The stock-take module is a PROPOSER, not a writer of inventory state.
The only path that mutates `CoinType.on_hand_qty` /
`OunceType.on_hand_qty` is `apply_unit_stock_adjustment_core` in
`app/api/adjustments.py`. Approving a stock-take variance calls that
core, which writes a ManualAdjustment row + a chained ledger event in
the same transaction as the stock-take workflow event. No shortcut, no
direct UPDATE of `on_hand_qty` from this module. ANY new path that
touches `on_hand_qty` outside `apply_unit_stock_adjustment_core` is a
code-review red flag.

STATE MACHINE
-------------
  DRAFT  ──submit──▶  SUBMITTED  ──per-line approve/reject──▶  CLOSED

Once SUBMITTED, each line's `expected_qty_at_submit` is frozen so the
variance the operator sees is the variance acted on, even if concurrent
sales mutate `on_hand_qty` afterwards. CLOSED is reached when every
line is resolved (APPROVED, REJECTED, or auto-set NO_VARIANCE).

CLOSE-RACE PROTECTION
---------------------
Approve/reject acquire `SELECT ... FOR UPDATE` on the parent stock_take
row BEFORE doing line-level work. This serializes all resolution
operations on the same stock-take, so two approvers resolving the last
two lines simultaneously cannot both emit STOCK_TAKE_CLOSED. The first
commits with status=CLOSED; the second sees CLOSED on parent re-read
and skips the close emission.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.adjustments import apply_unit_stock_adjustment_core
from app.core.ledger import (
    EVENT_STOCK_TAKE_CLOSED,
    EVENT_STOCK_TAKE_LINE_APPROVED,
    EVENT_STOCK_TAKE_LINE_REJECTED,
    EVENT_STOCK_TAKE_STARTED,
    EVENT_STOCK_TAKE_SUBMITTED,
    record,
)
from app.core.permissions import require_admin
from app.core.stock_take import to_adjustment_target
from app.deps import get_db
from app.models import (
    AdjustmentReason,
    CoinType,
    OunceType,
    StockTake,
    StockTakeLine,
    StockTakeLineResolution,
    StockTakeRefType,
    StockTakeStatus,
    User,
)
from app.schemas.stock_take import (
    StockTakeCreate,
    StockTakeLineCreate,
    StockTakeLineOut,
    StockTakeLineReject,
    StockTakeLineUpdate,
    StockTakeListItem,
    StockTakeListOut,
    StockTakeOut,
)

router = APIRouter(prefix="/stock-takes", tags=["stock-takes"])


# ── Helpers ──────────────────────────────────────────────────────────────────


def _parse_ref_type(value: str) -> StockTakeRefType:
    try:
        return StockTakeRefType(value)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid ref_type {value!r}; expected COIN_STOCK or OUNCE_STOCK",
        )


async def _get_target_on_hand_qty(
    db: AsyncSession, ref_type: StockTakeRefType, ref_id: str
) -> int:
    """Read the current on_hand_qty of the targeted coin/ounce type.

    Used at submit time to snapshot `expected_qty_at_submit`. Does NOT
    acquire a lock — the snapshot is intentionally a point-in-time read.
    Concurrent sales after submit are recorded separately (their own
    chained adjustment) and do not invalidate the variance the operator
    is approving.
    """
    if ref_type == StockTakeRefType.COIN_STOCK:
        row = (
            await db.execute(select(CoinType.on_hand_qty).where(CoinType.id == ref_id))
        ).one_or_none()
    else:
        row = (
            await db.execute(select(OunceType.on_hand_qty).where(OunceType.id == ref_id))
        ).one_or_none()
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"{ref_type.value} {ref_id} not found at submit time",
        )
    return int(row[0])


async def _load_take_or_404(db: AsyncSession, take_id: str) -> StockTake:
    take = (
        await db.execute(select(StockTake).where(StockTake.id == take_id))
    ).scalar_one_or_none()
    if not take:
        raise HTTPException(status_code=404, detail=f"Stock-take {take_id} not found")
    return take


async def _fresh_lines(db: AsyncSession, take_id: str) -> list[StockTakeLine]:
    """Always fetch lines via a fresh query rather than through the cached
    StockTake.lines relationship. The identity map can hold a stale
    (e.g. empty) lines collection from an earlier load in this session;
    `selectinload` does not re-fire when the relationship is already
    loaded. Forcing a fresh select sidesteps the issue without playing
    games with session.expire()."""
    return (
        await db.execute(
            select(StockTakeLine)
            .where(StockTakeLine.stock_take_id == take_id)
            .order_by(StockTakeLine.created_at)
        )
    ).scalars().all()


def _to_line_out(line: StockTakeLine) -> StockTakeLineOut:
    return StockTakeLineOut(
        id=line.id,
        stock_take_id=line.stock_take_id,
        ref_type=line.ref_type.value,
        ref_id=line.ref_id,
        counted_qty=line.counted_qty,
        expected_qty_at_submit=line.expected_qty_at_submit,
        variance=line.variance,
        resolution=line.resolution.value,
        rejection_reason=line.rejection_reason,
        adjustment_id=line.adjustment_id,
        resolved_at=line.resolved_at,
        resolved_by_user_id=line.resolved_by_user_id,
        created_at=line.created_at,
    )


def _to_out(take: StockTake, lines: list[StockTakeLine]) -> StockTakeOut:
    return StockTakeOut(
        id=take.id,
        started_at=take.started_at,
        started_by_user_id=take.started_by_user_id,
        submitted_at=take.submitted_at,
        closed_at=take.closed_at,
        status=take.status.value,
        notes=take.notes,
        lines=[_to_line_out(l) for l in lines],
    )


async def _maybe_close_take(
    db: AsyncSession, take: StockTake, actor_user_id: str
) -> None:
    """If every line on `take` is resolved, transition it to CLOSED and
    write STOCK_TAKE_CLOSED. Otherwise no-op.

    CALLER MUST already hold a FOR UPDATE lock on `take` — this function
    does not re-lock. The lock is what guarantees only one transaction
    can emit STOCK_TAKE_CLOSED for a given take.
    """
    if take.status == StockTakeStatus.CLOSED:
        return  # already closed by an earlier-arriving transaction (race lost)

    pending_count = (
        await db.execute(
            select(func.count())
            .select_from(StockTakeLine)
            .where(
                StockTakeLine.stock_take_id == take.id,
                StockTakeLine.resolution == StockTakeLineResolution.PENDING,
            )
        )
    ).scalar_one()
    if pending_count > 0:
        return

    # All lines resolved — close the take.
    take.status = StockTakeStatus.CLOSED
    take.closed_at = datetime.now(timezone.utc)

    # Summary counts for the ledger payload.
    counts = (
        await db.execute(
            select(StockTakeLine.resolution, func.count())
            .where(StockTakeLine.stock_take_id == take.id)
            .group_by(StockTakeLine.resolution)
        )
    ).all()
    by_resolution = {r.value: 0 for r in StockTakeLineResolution}
    for resolution, c in counts:
        by_resolution[resolution.value] = c

    await record(
        db,
        event_type=EVENT_STOCK_TAKE_CLOSED,
        actor_user_id=actor_user_id,
        ref_type="stock_take",
        ref_id=take.id,
        payload={
            "approved_count": by_resolution[StockTakeLineResolution.APPROVED.value],
            "rejected_count": by_resolution[StockTakeLineResolution.REJECTED.value],
            "no_variance_count": by_resolution[StockTakeLineResolution.NO_VARIANCE.value],
        },
    )


# ── Lifecycle endpoints ──────────────────────────────────────────────────────


@router.post("", response_model=StockTakeOut, status_code=201)
async def create_stock_take(
    body: StockTakeCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    take = StockTake(
        started_by_user_id=user.id,
        notes=body.notes,
    )
    db.add(take)
    await db.flush()
    await record(
        db,
        event_type=EVENT_STOCK_TAKE_STARTED,
        actor_user_id=user.id,
        ref_type="stock_take",
        ref_id=take.id,
        payload={"notes": body.notes},
    )
    await db.commit()
    # Reload with lines (empty list) for response.
    take = await _load_take_or_404(db, take.id)
    lines = await _fresh_lines(db, take.id)
    return _to_out(take, lines)


@router.get("", response_model=StockTakeListOut)
async def list_stock_takes(
    status_filter: str = Query(default="", alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    q = select(StockTake)
    if status_filter:
        try:
            q = q.where(StockTake.status == StockTakeStatus(status_filter))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid status {status_filter!r}")

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    q = (
        q.options(selectinload(StockTake.lines))
        .order_by(StockTake.started_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    takes = (await db.execute(q)).scalars().all()

    items: list[StockTakeListItem] = []
    for t in takes:
        line_count = len(t.lines)
        variance_line_count = sum(
            1 for l in t.lines if (l.variance is not None and l.variance != 0)
        )
        approved = sum(1 for l in t.lines if l.resolution == StockTakeLineResolution.APPROVED)
        rejected = sum(1 for l in t.lines if l.resolution == StockTakeLineResolution.REJECTED)
        items.append(StockTakeListItem(
            id=t.id,
            started_at=t.started_at,
            started_by_user_id=t.started_by_user_id,
            submitted_at=t.submitted_at,
            closed_at=t.closed_at,
            status=t.status.value,
            notes=t.notes,
            line_count=line_count,
            variance_line_count=variance_line_count,
            approved_count=approved,
            rejected_count=rejected,
        ))

    return StockTakeListOut(items=items, total=total, page=page, page_size=page_size)


@router.get("/{take_id}", response_model=StockTakeOut)
async def get_stock_take(
    take_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    take = await _load_take_or_404(db, take_id)
    lines = await _fresh_lines(db, take_id)
    return _to_out(take, lines)


# ── Line CRUD (DRAFT only) ───────────────────────────────────────────────────


@router.post("/{take_id}/lines", response_model=StockTakeLineOut, status_code=201)
async def add_line(
    take_id: str,
    body: StockTakeLineCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    take = await _load_take_or_404(db, take_id)
    if take.status != StockTakeStatus.DRAFT:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot add lines to stock-take in status {take.status.value}",
        )
    ref_type = _parse_ref_type(body.ref_type)
    # Verify the target exists so we fail fast with a clear error.
    await _get_target_on_hand_qty(db, ref_type, body.ref_id)

    line = StockTakeLine(
        stock_take_id=take.id,
        ref_type=ref_type,
        ref_id=body.ref_id,
        counted_qty=body.counted_qty,
    )
    db.add(line)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        # Most likely the unique index uq_stock_take_lines_unique_per_take fired.
        raise HTTPException(
            status_code=409,
            detail=f"A line for ({body.ref_type}, {body.ref_id}) already exists in this stock-take.",
        )
    await db.refresh(line)
    return _to_line_out(line)


@router.patch("/{take_id}/lines/{line_id}", response_model=StockTakeLineOut)
async def edit_line(
    take_id: str,
    line_id: str,
    body: StockTakeLineUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    take = await _load_take_or_404(db, take_id)
    if take.status != StockTakeStatus.DRAFT:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot edit lines on stock-take in status {take.status.value}",
        )
    line = (
        await db.execute(
            select(StockTakeLine).where(
                StockTakeLine.id == line_id,
                StockTakeLine.stock_take_id == take_id,
            )
        )
    ).scalar_one_or_none()
    if not line:
        raise HTTPException(status_code=404, detail=f"Line {line_id} not found")

    line.counted_qty = body.counted_qty
    await db.commit()
    await db.refresh(line)
    return _to_line_out(line)


@router.delete("/{take_id}/lines/{line_id}", status_code=204)
async def remove_line(
    take_id: str,
    line_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    take = await _load_take_or_404(db, take_id)
    if take.status != StockTakeStatus.DRAFT:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete lines on stock-take in status {take.status.value}",
        )
    line = (
        await db.execute(
            select(StockTakeLine).where(
                StockTakeLine.id == line_id,
                StockTakeLine.stock_take_id == take_id,
            )
        )
    ).scalar_one_or_none()
    if not line:
        raise HTTPException(status_code=404, detail=f"Line {line_id} not found")
    await db.delete(line)
    await db.commit()


# ── Submit (freeze expected_qty + compute variances) ─────────────────────────


@router.post("/{take_id}/submit", response_model=StockTakeOut)
async def submit_stock_take(
    take_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Freeze expected_qty per line, compute variance, mark SUBMITTED.

    Zero-variance lines auto-resolve to NO_VARIANCE. If EVERY line is
    zero-variance, the stock-take closes immediately (no human review
    needed). Otherwise SUBMITTED waits for per-line approve/reject.
    """
    take = (
        await db.execute(
            select(StockTake).where(StockTake.id == take_id).with_for_update()
        )
    ).scalar_one_or_none()
    if not take:
        raise HTTPException(status_code=404, detail=f"Stock-take {take_id} not found")
    if take.status != StockTakeStatus.DRAFT:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot submit stock-take in status {take.status.value}",
        )

    # Fetch lines via a fresh query rather than `take.lines`. The
    # relationship may have been loaded (and cached as empty) earlier in
    # this session, before lines were added.
    lines = (
        await db.execute(
            select(StockTakeLine).where(StockTakeLine.stock_take_id == take_id)
        )
    ).scalars().all()

    if not lines:
        raise HTTPException(
            status_code=422,
            detail="Cannot submit a stock-take with zero lines",
        )

    variance_count = 0
    total_variance_abs = 0
    for line in lines:
        expected = await _get_target_on_hand_qty(db, line.ref_type, line.ref_id)
        line.expected_qty_at_submit = expected
        line.variance = line.counted_qty - expected
        if line.variance == 0:
            line.resolution = StockTakeLineResolution.NO_VARIANCE
            line.resolved_at = datetime.now(timezone.utc)
            line.resolved_by_user_id = user.id
        else:
            variance_count += 1
            total_variance_abs += abs(line.variance)

    take.status = StockTakeStatus.SUBMITTED
    take.submitted_at = datetime.now(timezone.utc)

    await record(
        db,
        event_type=EVENT_STOCK_TAKE_SUBMITTED,
        actor_user_id=user.id,
        ref_type="stock_take",
        ref_id=take.id,
        payload={
            "line_count": len(lines),
            "variance_count": variance_count,
            "total_variance_abs": total_variance_abs,
        },
    )

    # If every line was zero-variance, auto-close in the same transaction.
    await _maybe_close_take(db, take, user.id)

    await db.commit()
    take = await _load_take_or_404(db, take.id)
    lines = await _fresh_lines(db, take.id)
    return _to_out(take, lines)


# ── Approve / Reject per line ────────────────────────────────────────────────


@router.post("/{take_id}/lines/{line_id}/approve", response_model=StockTakeLineOut)
async def approve_line(
    take_id: str,
    line_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Approve a variance.

    Order of locks (avoids the classic deadlock from approvers grabbing
    parent and line in different orders):
      1. Parent stock_take FOR UPDATE.
      2. Then the line itself.
    The parent lock serializes ALL resolution ops on the same stock-take,
    which is what makes the close-step safe under concurrency.
    """
    take = (
        await db.execute(
            select(StockTake).where(StockTake.id == take_id).with_for_update()
        )
    ).scalar_one_or_none()
    if not take:
        raise HTTPException(status_code=404, detail=f"Stock-take {take_id} not found")
    if take.status != StockTakeStatus.SUBMITTED:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot approve lines on stock-take in status {take.status.value}",
        )

    line = (
        await db.execute(
            select(StockTakeLine).where(
                StockTakeLine.id == line_id,
                StockTakeLine.stock_take_id == take_id,
            )
        )
    ).scalar_one_or_none()
    if not line:
        raise HTTPException(status_code=404, detail=f"Line {line_id} not found")
    if line.resolution != StockTakeLineResolution.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"Line is already {line.resolution.value}; cannot approve.",
        )
    if line.variance is None or line.variance == 0:
        # Defensive: zero-variance lines are auto-resolved to NO_VARIANCE
        # at submit; this branch should be unreachable.
        raise HTTPException(
            status_code=409,
            detail="Line has no variance to approve (should have been NO_VARIANCE).",
        )

    # Apply the variance through the SINGLE on_hand_qty mutation path.
    # The mapping (StockTakeRefType -> AdjustmentTarget) is explicit and
    # tested in tests/test_stock_take_ref_type_mapping.py.
    adjustment_target = to_adjustment_target(line.ref_type)
    from decimal import Decimal as _D
    adjustment = await apply_unit_stock_adjustment_core(
        db,
        target_type=adjustment_target,
        target_id=line.ref_id,
        delta=_D(str(line.variance)),
        reason=AdjustmentReason.CORRECTION,
        notes=f"Stock-take {take.id} approval (line {line.id})",
        actor_user_id=user.id,
        ledger_extra={"stock_take_line_id": line.id},
    )

    # Mark the line APPROVED and FK-link the adjustment.
    line.resolution = StockTakeLineResolution.APPROVED
    line.adjustment_id = adjustment.id
    line.resolved_at = datetime.now(timezone.utc)
    line.resolved_by_user_id = user.id

    # Workflow-level event (separate from the inventory-level event
    # apply_unit_stock_adjustment_core just wrote — both appear in the
    # ledger, cross-referenced via adjustment_id and stock_take_line_id).
    await record(
        db,
        event_type=EVENT_STOCK_TAKE_LINE_APPROVED,
        actor_user_id=user.id,
        ref_type="stock_take_line",
        ref_id=line.id,
        payload={
            "stock_take_id": take.id,
            "ref_type": line.ref_type.value,
            "ref_id": line.ref_id,
            "counted_qty": line.counted_qty,
            "expected_qty": line.expected_qty_at_submit,
            "variance": line.variance,
            "adjustment_id": adjustment.id,
        },
    )

    # If this was the last pending line, close. Safe because parent lock
    # is held.
    await _maybe_close_take(db, take, user.id)

    await db.commit()
    await db.refresh(line)
    return _to_line_out(line)


@router.post("/{take_id}/lines/{line_id}/reject", response_model=StockTakeLineOut)
async def reject_line(
    take_id: str,
    line_id: str,
    body: StockTakeLineReject,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Reject a variance with a reason. on_hand_qty unchanged.

    The rejection is recorded in the ledger so an auditor can answer
    "we are knowingly wrong by N — why?". The next reconcile-units run
    will continue to report the drift (B1 endpoint is the live source
    of truth for "is the system currently wrong?").
    """
    take = (
        await db.execute(
            select(StockTake).where(StockTake.id == take_id).with_for_update()
        )
    ).scalar_one_or_none()
    if not take:
        raise HTTPException(status_code=404, detail=f"Stock-take {take_id} not found")
    if take.status != StockTakeStatus.SUBMITTED:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot reject lines on stock-take in status {take.status.value}",
        )

    line = (
        await db.execute(
            select(StockTakeLine).where(
                StockTakeLine.id == line_id,
                StockTakeLine.stock_take_id == take_id,
            )
        )
    ).scalar_one_or_none()
    if not line:
        raise HTTPException(status_code=404, detail=f"Line {line_id} not found")
    if line.resolution != StockTakeLineResolution.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"Line is already {line.resolution.value}; cannot reject.",
        )

    line.resolution = StockTakeLineResolution.REJECTED
    line.rejection_reason = body.reason
    line.resolved_at = datetime.now(timezone.utc)
    line.resolved_by_user_id = user.id

    await record(
        db,
        event_type=EVENT_STOCK_TAKE_LINE_REJECTED,
        actor_user_id=user.id,
        ref_type="stock_take_line",
        ref_id=line.id,
        payload={
            "stock_take_id": take.id,
            "ref_type": line.ref_type.value,
            "ref_id": line.ref_id,
            "counted_qty": line.counted_qty,
            "expected_qty": line.expected_qty_at_submit,
            "variance": line.variance,
            "reason": body.reason,
        },
    )

    await _maybe_close_take(db, take, user.id)

    await db.commit()
    await db.refresh(line)
    return _to_line_out(line)
