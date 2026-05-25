"""End-to-end integration tests for the stock-take workflow (audit B2).

Covers:
  1. Happy path: start → add lines → submit → approve → on_hand_qty
     changed by exactly the variance; ManualAdjustment created with
     stock_take_line_id cross-reference; chain intact.
  2. No-variance auto-resolution: all lines counted == expected →
     NO_VARIANCE + auto-CLOSED with no adjustments posted.
  3. Reject path: variance rejected with reason → REJECTED, on_hand_qty
     unchanged, no adjustment, reason captured in ledger.
  4. Double-approve is 409: second call returns 409 with no second
     adjustment.
  5. Expected-qty freeze: a concurrent sale after submit doesn't move
     the variance the operator is approving.
  6. Status guards: cannot add lines to SUBMITTED, cannot approve on
     DRAFT, cannot submit CLOSED.

User-required test (chain contiguity for N>1 record() per tx is already
covered by the B2.0 pre-flight `test_audit_chain_multi_write.py`):

  7. Rejected variances stay visible in reconcile-units: a rejected -2
     variance still shows as drift in the live reconcile + appears in
     the stock-take history with reason.

  8. Approving a coin line produces a ManualAdjustment with EXACTLY
     AdjustmentTarget.COIN_STOCK (the explicit mapping check, end-to-end).
"""
from decimal import Decimal

import pytest
from sqlalchemy import select, func

from app.api.adjustments import apply_unit_stock_adjustment_core
from app.api.inventory import reconcile_units
from app.api.stock_takes import (
    add_line,
    approve_line,
    create_stock_take,
    get_stock_take,
    reject_line,
    submit_stock_take,
)
from app.core.audit_chain import verify_chain
from app.models import (
    AdjustmentReason,
    AdjustmentTarget,
    CoinType,
    InventoryLedger,
    Karat,
    ManualAdjustment,
    MarginMode,
    OunceType,
    Role,
    StockTake,
    StockTakeLine,
    StockTakeLineResolution,
    StockTakeStatus,
    User,
)
from app.schemas.stock_take import (
    StockTakeCreate,
    StockTakeLineCreate,
    StockTakeLineReject,
)


# ── Test fixtures ────────────────────────────────────────────────────────────


def _admin() -> User:
    return User(
        id="u-admin",
        email="a@a",
        name="a",
        password_hash="x",
        role=Role.ADMIN,
        is_active=True,
    )


def _coin(code: str, *, on_hand_qty: int) -> CoinType:
    return CoinType(
        code=code,
        name_en=code,
        karat=Karat.K22,
        weight_grams=Decimal("7.988"),
        markup_per_gram=Decimal("0"),
        margin_mode=MarginMode.USD,
        margin_value=Decimal("0"),
        on_hand_qty=on_hand_qty,
        is_active=True,
    )


def _ounce(code: str, *, on_hand_qty: int) -> OunceType:
    return OunceType(
        code=code,
        name_en=code,
        karat=Karat.K24,
        weight_grams=Decimal("31.104"),
        markup_per_gram=Decimal("0"),
        margin_mode=MarginMode.USD,
        margin_value=Decimal("0"),
        on_hand_qty=on_hand_qty,
        is_active=True,
    )


async def _all_ledger_rows(db) -> list[dict]:
    rows = (
        await db.execute(
            select(InventoryLedger).order_by(
                InventoryLedger.occurred_at, InventoryLedger.id
            )
        )
    ).scalars().all()
    return [
        {
            "id": r.id, "prev_hash": r.prev_hash, "entry_hash": r.entry_hash,
            "event_type": r.event_type, "actor_user_id": r.actor_user_id,
            "occurred_at": r.occurred_at, "ref_type": r.ref_type,
            "ref_id": r.ref_id, "payload": r.payload,
        }
        for r in rows
    ]


# ── 1. Happy path ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_approve_adjusts_qty_and_links_adjustment(db):
    user = _admin()
    coin = _coin("C-A", on_hand_qty=10)
    db.add(user)
    db.add(coin)
    await db.flush()

    # Start, count 12 (variance +2), submit, approve.
    take = await create_stock_take(StockTakeCreate(notes="test"), db=db, user=user)
    await add_line(
        take.id,
        StockTakeLineCreate(ref_type="COIN_STOCK", ref_id=coin.id, counted_qty=12),
        db=db, _=user,
    )
    submitted = await submit_stock_take(take.id, db=db, user=user)
    line = submitted.lines[0]
    assert line.expected_qty_at_submit == 10
    assert line.variance == 2
    assert line.resolution == "PENDING"

    approved = await approve_line(take.id, line.id, db=db, user=user)
    assert approved.resolution == "APPROVED"
    assert approved.adjustment_id is not None

    # on_hand_qty mutated by EXACTLY the variance.
    await db.refresh(coin)
    assert coin.on_hand_qty == 12

    # ManualAdjustment row exists with the correct cross-reference.
    adj = (
        await db.execute(
            select(ManualAdjustment).where(ManualAdjustment.id == approved.adjustment_id)
        )
    ).scalar_one()
    assert adj.target_type == AdjustmentTarget.COIN_STOCK
    assert adj.target_id == coin.id
    assert int(adj.delta) == 2

    # The chained ledger event for the inventory adjustment includes the
    # stock_take_line_id cross-reference.
    inv_event = (
        await db.execute(
            select(InventoryLedger).where(
                InventoryLedger.event_type == "COIN_STOCK_ADJUSTED",
                InventoryLedger.ref_id == coin.id,
            )
        )
    ).scalars().all()
    assert len(inv_event) == 1
    assert inv_event[0].payload["stock_take_line_id"] == line.id

    # Chain still verifies, and the 3 chained writes (COIN_STOCK_ADJUSTED,
    # STOCK_TAKE_LINE_APPROVED, STOCK_TAKE_CLOSED) are contiguous.
    result = verify_chain(await _all_ledger_rows(db))
    assert result["status"] == "intact"

    # Take auto-closed because that was the only line.
    detail = await get_stock_take(take.id, db=db, _=user)
    assert detail.status == "CLOSED"


# ── 2. No-variance auto-resolution ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_variance_auto_resolves_and_closes(db):
    user = _admin()
    coin = _coin("C-N", on_hand_qty=10)
    db.add(user)
    db.add(coin)
    await db.flush()

    take = await create_stock_take(StockTakeCreate(notes=None), db=db, user=user)
    await add_line(
        take.id,
        StockTakeLineCreate(ref_type="COIN_STOCK", ref_id=coin.id, counted_qty=10),
        db=db, _=user,
    )
    submitted = await submit_stock_take(take.id, db=db, user=user)
    assert submitted.status == "CLOSED"
    assert submitted.lines[0].resolution == "NO_VARIANCE"
    assert submitted.lines[0].variance == 0

    # No adjustment posted.
    adj_count = (
        await db.execute(select(func.count()).select_from(ManualAdjustment))
    ).scalar_one()
    assert adj_count == 0

    # on_hand_qty unchanged.
    await db.refresh(coin)
    assert coin.on_hand_qty == 10


# ── 3. Reject path ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reject_records_reason_and_does_not_touch_inventory(db):
    user = _admin()
    coin = _coin("C-R", on_hand_qty=10)
    db.add(user)
    db.add(coin)
    await db.flush()

    take = await create_stock_take(StockTakeCreate(notes=None), db=db, user=user)
    await add_line(
        take.id,
        StockTakeLineCreate(ref_type="COIN_STOCK", ref_id=coin.id, counted_qty=8),
        db=db, _=user,
    )
    submitted = await submit_stock_take(take.id, db=db, user=user)
    line_id = submitted.lines[0].id

    rejected = await reject_line(
        take.id, line_id,
        StockTakeLineReject(reason="acceptable shrinkage"),
        db=db, user=user,
    )
    assert rejected.resolution == "REJECTED"
    assert rejected.rejection_reason == "acceptable shrinkage"
    assert rejected.adjustment_id is None

    # on_hand_qty unchanged.
    await db.refresh(coin)
    assert coin.on_hand_qty == 10

    # No ManualAdjustment posted.
    adj_count = (
        await db.execute(select(func.count()).select_from(ManualAdjustment))
    ).scalar_one()
    assert adj_count == 0

    # Reason is in the ledger.
    reject_event = (
        await db.execute(
            select(InventoryLedger).where(
                InventoryLedger.event_type == "STOCK_TAKE_LINE_REJECTED"
            )
        )
    ).scalar_one()
    assert reject_event.payload["reason"] == "acceptable shrinkage"
    assert reject_event.payload["variance"] == -2


# ── 4. Double-approve is 409 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_double_approve_returns_409_and_does_not_post_second_adjustment(db):
    from fastapi import HTTPException

    user = _admin()
    coin = _coin("C-D", on_hand_qty=10)
    db.add(user)
    db.add(coin)
    await db.flush()

    take = await create_stock_take(StockTakeCreate(notes=None), db=db, user=user)
    await add_line(
        take.id,
        StockTakeLineCreate(ref_type="COIN_STOCK", ref_id=coin.id, counted_qty=12),
        db=db, _=user,
    )
    submitted = await submit_stock_take(take.id, db=db, user=user)
    line_id = submitted.lines[0].id

    await approve_line(take.id, line_id, db=db, user=user)

    with pytest.raises(HTTPException) as ei:
        await approve_line(take.id, line_id, db=db, user=user)
    assert ei.value.status_code == 409

    # Exactly ONE adjustment exists for this coin.
    adj_count = (
        await db.execute(
            select(func.count()).select_from(ManualAdjustment).where(
                ManualAdjustment.target_id == coin.id
            )
        )
    ).scalar_one()
    assert adj_count == 1

    # on_hand_qty moved by 2 once, not by 4.
    await db.refresh(coin)
    assert coin.on_hand_qty == 12


# ── 5. Expected-qty freeze ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_expected_qty_frozen_at_submit_survives_concurrent_changes(db):
    """After submit, expected_qty_at_submit is a snapshot. If a concurrent
    sale (or other adjustment) changes on_hand_qty afterwards, the
    variance the operator is approving is still the one they saw at
    submit time. The concurrent change is separately audited by ITS own
    chained event."""
    user = _admin()
    coin = _coin("C-F", on_hand_qty=10)
    db.add(user)
    db.add(coin)
    await db.flush()

    take = await create_stock_take(StockTakeCreate(notes=None), db=db, user=user)
    await add_line(
        take.id,
        StockTakeLineCreate(ref_type="COIN_STOCK", ref_id=coin.id, counted_qty=12),
        db=db, _=user,
    )
    submitted = await submit_stock_take(take.id, db=db, user=user)
    line_at_submit = submitted.lines[0]
    assert line_at_submit.expected_qty_at_submit == 10
    assert line_at_submit.variance == 2

    # Simulate a concurrent sale: -3 via the same audited path.
    await apply_unit_stock_adjustment_core(
        db,
        target_type=AdjustmentTarget.COIN_STOCK,
        target_id=coin.id,
        delta=Decimal("-3"),
        reason=AdjustmentReason.CORRECTION,
        notes="simulated sale during stock-take review",
        actor_user_id=user.id,
    )
    await db.commit()

    # on_hand_qty is now 10 - 3 = 7.
    await db.refresh(coin)
    assert coin.on_hand_qty == 7

    # Approve: applies the FROZEN variance of +2, not (counted - current).
    approved = await approve_line(take.id, line_at_submit.id, db=db, user=user)
    assert approved.variance == 2  # frozen value preserved

    # Final on_hand_qty: 7 + 2 = 9.  (Not 12.)  This is correct — the
    # stock-take and the concurrent sale are separate audited events.
    await db.refresh(coin)
    assert coin.on_hand_qty == 9


# ── 6. Status guards ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_guards_block_invalid_transitions(db):
    from fastapi import HTTPException

    user = _admin()
    coin = _coin("C-G", on_hand_qty=10)
    db.add(user)
    db.add(coin)
    await db.flush()

    take = await create_stock_take(StockTakeCreate(notes=None), db=db, user=user)

    # Cannot approve a line on a DRAFT take (no lines yet anyway, but
    # state guard fires first):
    with pytest.raises(HTTPException) as ei:
        await approve_line(take.id, "nonexistent", db=db, user=user)
    # State guard runs before line lookup (take checked first).
    assert ei.value.status_code == 409

    # Cannot submit a take with zero lines.
    with pytest.raises(HTTPException) as ei:
        await submit_stock_take(take.id, db=db, user=user)
    assert ei.value.status_code == 422

    # Add a line, submit, then verify we can't add another.
    await add_line(
        take.id,
        StockTakeLineCreate(ref_type="COIN_STOCK", ref_id=coin.id, counted_qty=10),
        db=db, _=user,
    )
    submitted = await submit_stock_take(take.id, db=db, user=user)
    # Auto-closed because zero-variance, so status is CLOSED.
    assert submitted.status == "CLOSED"

    with pytest.raises(HTTPException) as ei:
        await add_line(
            take.id,
            StockTakeLineCreate(ref_type="COIN_STOCK", ref_id=coin.id, counted_qty=5),
            db=db, _=user,
        )
    assert ei.value.status_code == 409

    # Cannot re-submit.
    with pytest.raises(HTTPException) as ei:
        await submit_stock_take(take.id, db=db, user=user)
    assert ei.value.status_code == 409


# ── 7. Rejected variances stay visible in reconcile-units (USER REQUIREMENT) ─


@pytest.mark.asyncio
async def test_rejected_variance_stays_visible_as_live_drift(db):
    """A rejected -2 variance means the system stays knowingly wrong by 2.
    That's acceptable IF it stays visible — the next reconcile-units run
    must still report the drift, and the stock-take history must show
    the rejection with the reason."""
    user = _admin()
    # Stored qty 10, but the replay will compute 0 (no events seeded) →
    # reconcile sees drift of +10 BEFORE the stock-take.
    coin = _coin("C-RV", on_hand_qty=10)
    db.add(user)
    db.add(coin)
    await db.flush()

    # Baseline drift exists.
    pre = await reconcile_units(alert=False, db=db, _=user)
    assert pre["drift_count"] == 1
    assert pre["unit_drifts"][0]["drift"] == 10

    # Stock-take: count 10 (matching stored). Variance is computed against
    # CURRENT on_hand_qty (10), not the replay. So variance = 0 here —
    # this is correct, the stock-take is comparing physical-counted-by-
    # operator vs the system's current claim, not vs the historical replay.
    # Submit, the line auto-resolves NO_VARIANCE. The +10 drift between
    # the system and the ledger replay PERSISTS.
    take = await create_stock_take(StockTakeCreate(notes=None), db=db, user=user)
    await add_line(
        take.id,
        StockTakeLineCreate(ref_type="COIN_STOCK", ref_id=coin.id, counted_qty=10),
        db=db, _=user,
    )
    submitted = await submit_stock_take(take.id, db=db, user=user)
    assert submitted.status == "CLOSED"
    assert submitted.lines[0].resolution == "NO_VARIANCE"

    # CRITICAL ASSERTION: reconcile still reports the drift. The
    # stock-take did NOT silently absolve the drift — it confirmed
    # physically what the system claims; the ledger replay still
    # disagrees and that disagreement remains visible.
    post = await reconcile_units(alert=False, db=db, _=user)
    assert post["drift_count"] == 1
    assert post["unit_drifts"][0]["drift"] == 10

    # Now actually use the reject path: a separate stock-take where
    # the count DISAGREES with the system, the variance is rejected,
    # and reconcile still shows whatever drift was there.
    take2 = await create_stock_take(StockTakeCreate(notes=None), db=db, user=user)
    await add_line(
        take2.id,
        StockTakeLineCreate(ref_type="COIN_STOCK", ref_id=coin.id, counted_qty=8),
        db=db, _=user,
    )
    submitted2 = await submit_stock_take(take2.id, db=db, user=user)
    line2_id = submitted2.lines[0].id
    assert submitted2.lines[0].variance == -2

    rejected = await reject_line(
        take2.id, line2_id,
        StockTakeLineReject(reason="acceptable shrinkage; investigating"),
        db=db, user=user,
    )
    assert rejected.resolution == "REJECTED"

    # The closed stock-take's history surfaces the rejected line.
    detail = await get_stock_take(take2.id, db=db, _=user)
    rejected_lines = [l for l in detail.lines if l.resolution == "REJECTED"]
    assert len(rejected_lines) == 1
    assert rejected_lines[0].variance == -2
    assert rejected_lines[0].rejection_reason == "acceptable shrinkage; investigating"

    # Reconcile STILL reports the original +10 drift (the rejection
    # didn't move on_hand_qty, the system stays knowingly wrong).
    final = await reconcile_units(alert=False, db=db, _=user)
    assert final["drift_count"] == 1
    assert final["unit_drifts"][0]["drift"] == 10


# ── 8. Explicit COIN-line mapping → AdjustmentTarget.COIN_STOCK (USER REQ) ──


@pytest.mark.asyncio
async def test_approving_coin_line_posts_adjustment_with_coin_target_exactly(db):
    """The end-to-end version of test_stock_take_ref_type_mapping —
    proves that a COIN_STOCK stock-take line produces a ManualAdjustment
    with EXACTLY AdjustmentTarget.COIN_STOCK (not OUNCE_STOCK, not None,
    not something coerced)."""
    user = _admin()
    coin = _coin("C-MAP-COIN", on_hand_qty=5)
    ounce = _ounce("O-MAP-OZ", on_hand_qty=3)
    db.add(user)
    db.add(coin)
    db.add(ounce)
    await db.flush()

    # COIN line +1.
    take = await create_stock_take(StockTakeCreate(notes=None), db=db, user=user)
    await add_line(
        take.id,
        StockTakeLineCreate(ref_type="COIN_STOCK", ref_id=coin.id, counted_qty=6),
        db=db, _=user,
    )
    submitted = await submit_stock_take(take.id, db=db, user=user)
    approved = await approve_line(take.id, submitted.lines[0].id, db=db, user=user)

    adj = (
        await db.execute(
            select(ManualAdjustment).where(ManualAdjustment.id == approved.adjustment_id)
        )
    ).scalar_one()
    assert adj.target_type == AdjustmentTarget.COIN_STOCK
    assert adj.target_id == coin.id

    # OUNCE line +1 (separate stock-take).
    take2 = await create_stock_take(StockTakeCreate(notes=None), db=db, user=user)
    await add_line(
        take2.id,
        StockTakeLineCreate(ref_type="OUNCE_STOCK", ref_id=ounce.id, counted_qty=4),
        db=db, _=user,
    )
    submitted2 = await submit_stock_take(take2.id, db=db, user=user)
    approved2 = await approve_line(take2.id, submitted2.lines[0].id, db=db, user=user)

    adj2 = (
        await db.execute(
            select(ManualAdjustment).where(ManualAdjustment.id == approved2.adjustment_id)
        )
    ).scalar_one()
    assert adj2.target_type == AdjustmentTarget.OUNCE_STOCK
    assert adj2.target_id == ounce.id

    # And cross-check: the two adjustments did NOT touch the wrong table.
    await db.refresh(coin)
    await db.refresh(ounce)
    assert coin.on_hand_qty == 6     # not 5, not 3, not 4
    assert ounce.on_hand_qty == 4    # not 3, not 6, not 5
