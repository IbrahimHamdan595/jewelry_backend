from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ledger import EVENT_ZAKAT_SNAPSHOT_CREATED, record
from app.core.permissions import require_admin
from app.core.zakat import (
    ZakatSummary,
    compute_integrity_hash,
    compute_zakat_summary,
)
from app.deps import get_db
from app.models import User, ZakatSnapshot
from app.schemas.zakat import (
    KaratBucketOut,
    ZakatHoldingsOut,
    ZakatSnapshotCreate,
    ZakatSnapshotListOut,
    ZakatSnapshotOut,
    ZakatSummaryOut,
)

router = APIRouter(prefix="/zakat", tags=["zakat"], dependencies=[Depends(require_admin)])


def _to_out(summary: ZakatSummary) -> ZakatSummaryOut:
    return ZakatSummaryOut(
        holdings=ZakatHoldingsOut(
            by_karat=[
                KaratBucketOut(
                    karat=b.karat.value,
                    grams_by_source=b.grams_by_source,
                    total_weight_grams=b.total_weight_grams,
                    au_grams=b.au_grams,
                )
                for b in summary.holdings.by_karat
            ],
            total_au_grams=summary.holdings.total_au_grams,
        ),
        gold_rate_24k=summary.gold_rate_24k,
        gold_rate_source=summary.gold_rate_source,
        gold_rate_is_stale=summary.gold_rate_is_stale,
        gold_rate_fetched_at=summary.gold_rate_fetched_at,
        nisab_grams=summary.nisab_grams,
        meets_nisab=summary.meets_nisab,
        total_au_value_usd=summary.total_au_value_usd,
        zakat_au_grams=summary.zakat_au_grams,
        zakat_value_usd=summary.zakat_value_usd,
    )


def _breakdown_to_json(summary: ZakatSummary) -> dict:
    """Serialize the per-karat breakdown for storage in the JSON column.
    Decimals → strings so JSON encoding doesn't drop trailing zeros.
    """
    out: dict = {}
    for bucket in summary.holdings.by_karat:
        out[bucket.karat.value] = {
            "products": str(bucket.grams_by_source["products"]),
            "coins": str(bucket.grams_by_source["coins"]),
            "ounces": str(bucket.grams_by_source["ounces"]),
            "lots": str(bucket.grams_by_source["lots"]),
            "total_weight_grams": str(bucket.total_weight_grams),
            "au_grams": str(bucket.au_grams),
        }
    return out


def _snapshot_integrity_fields(row: ZakatSnapshot) -> dict:
    """Extract the hash-relevant fields from a persisted snapshot row.
    Used both at write time (hash to store) and at read time (recompute &
    compare for tamper detection)."""
    return {
        "assessment_date": row.assessment_date,
        "gold_rate_24k_usd_per_gram": row.gold_rate_24k_usd_per_gram,
        "gold_rate_source": row.gold_rate_source,
        "nisab_grams_used": row.nisab_grams_used,
        "total_au_grams": row.total_au_grams,
        "total_au_value_usd": row.total_au_value_usd,
        "zakat_au_grams": row.zakat_au_grams,
        "zakat_value_usd": row.zakat_value_usd,
        "meets_nisab": row.meets_nisab,
        "breakdown_by_karat": row.breakdown_by_karat,
    }


def _snapshot_to_out(row: ZakatSnapshot) -> ZakatSnapshotOut:
    recomputed = compute_integrity_hash(_snapshot_integrity_fields(row))
    return ZakatSnapshotOut(
        id=row.id,
        taken_at=row.taken_at,
        assessment_date=row.assessment_date,
        taken_by_user_id=row.taken_by_user_id,
        notes=row.notes,
        gold_rate_24k_usd_per_gram=row.gold_rate_24k_usd_per_gram,
        gold_rate_source=row.gold_rate_source,
        nisab_grams_used=row.nisab_grams_used,
        total_au_grams=row.total_au_grams,
        total_au_value_usd=row.total_au_value_usd,
        zakat_au_grams=row.zakat_au_grams,
        zakat_value_usd=row.zakat_value_usd,
        meets_nisab=row.meets_nisab,
        breakdown_by_karat=row.breakdown_by_karat,
        integrity_hash=row.integrity_hash,
        integrity_ok=(recomputed == row.integrity_hash),
    )


# ── Live ──────────────────────────────────────────────────────────────────────

@router.get("", response_model=ZakatSummaryOut)
async def live_zakat(db: AsyncSession = Depends(get_db)):
    """Live zakat summary recomputed from current inventory on every request."""
    try:
        summary = await compute_zakat_summary(db)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return _to_out(summary)


# ── Snapshots (immutable, append-only) ────────────────────────────────────────

@router.post("/snapshots", response_model=ZakatSnapshotOut, status_code=201)
async def create_snapshot(
    body: ZakatSnapshotCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Recompute the live summary and persist it as an immutable snapshot.

    Duplicates per assessment_date are permitted by design; the UI shows
    latest-per-date with an "all" toggle.
    """
    try:
        summary = await compute_zakat_summary(db)
    except RuntimeError as e:
        # Refuse to take a snapshot if no rate is available — a snapshot
        # without a rate is worthless for audit purposes.
        raise HTTPException(status_code=503, detail=str(e))

    breakdown = _breakdown_to_json(summary)

    fields_for_hash = {
        "assessment_date": body.assessment_date,
        "gold_rate_24k_usd_per_gram": summary.gold_rate_24k,
        "gold_rate_source": summary.gold_rate_source,
        "nisab_grams_used": summary.nisab_grams,
        "total_au_grams": summary.holdings.total_au_grams,
        "total_au_value_usd": summary.total_au_value_usd,
        "zakat_au_grams": summary.zakat_au_grams,
        "zakat_value_usd": summary.zakat_value_usd,
        "meets_nisab": summary.meets_nisab,
        "breakdown_by_karat": breakdown,
    }
    integrity = compute_integrity_hash(fields_for_hash)

    snapshot = ZakatSnapshot(
        assessment_date=body.assessment_date,
        taken_by_user_id=user.id,
        notes=body.notes,
        gold_rate_24k_usd_per_gram=summary.gold_rate_24k,
        gold_rate_source=summary.gold_rate_source,
        nisab_grams_used=summary.nisab_grams,
        total_au_grams=summary.holdings.total_au_grams,
        total_au_value_usd=summary.total_au_value_usd,
        zakat_au_grams=summary.zakat_au_grams,
        zakat_value_usd=summary.zakat_value_usd,
        meets_nisab=summary.meets_nisab,
        breakdown_by_karat=breakdown,
        integrity_hash=integrity,
    )
    db.add(snapshot)
    await db.flush()

    # Cross-trail audit event so the snapshot also shows up in /api/ledger.
    await record(
        db,
        event_type=EVENT_ZAKAT_SNAPSHOT_CREATED,
        actor_user_id=user.id,
        ref_type="zakat_snapshot",
        ref_id=snapshot.id,
        payload={
            "assessment_date": body.assessment_date.isoformat(),
            "total_au_grams": str(summary.holdings.total_au_grams),
            "zakat_au_grams": str(summary.zakat_au_grams),
            "zakat_value_usd": str(summary.zakat_value_usd),
            "gold_rate_24k": str(summary.gold_rate_24k),
            "gold_rate_source": summary.gold_rate_source,
            "integrity_hash": integrity,
        },
    )

    await db.commit()
    await db.refresh(snapshot)
    return _snapshot_to_out(snapshot)


@router.get("/snapshots", response_model=ZakatSnapshotListOut)
async def list_snapshots(
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    from_date: date | None = Query(default=None, description="Inclusive lower bound on assessment_date"),
    to_date: date | None = Query(default=None, description="Inclusive upper bound on assessment_date"),
):
    """List snapshots, newest first. Paged. Optional assessment_date filter."""
    q = select(ZakatSnapshot)
    if from_date is not None:
        q = q.where(ZakatSnapshot.assessment_date >= from_date)
    if to_date is not None:
        q = q.where(ZakatSnapshot.assessment_date <= to_date)

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()

    q = q.order_by(ZakatSnapshot.taken_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(q)).scalars().all()
    return ZakatSnapshotListOut(items=[_snapshot_to_out(r) for r in rows], total=total)


@router.get("/snapshots/{snapshot_id}", response_model=ZakatSnapshotOut)
async def get_snapshot(snapshot_id: str, db: AsyncSession = Depends(get_db)):
    row = (
        await db.execute(select(ZakatSnapshot).where(ZakatSnapshot.id == snapshot_id))
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return _snapshot_to_out(row)


# Deliberately no PATCH or DELETE — snapshots are immutable, append-only.
