from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ledger import EVENT_LOT_CREATED, record
from app.core.permissions import require_admin
from app.deps import get_db
from app.models import GoldLot, Karat, LotSource, User
from app.schemas.lot import (
    LotCreate, LotKaratTotal, LotListOut, LotOut, LotTotalsOut, LotUpdate,
)

router = APIRouter(prefix="/lots", tags=["lots"])


_API_CREATABLE_SOURCES = {LotSource.SEED, LotSource.ADJUSTMENT}


@router.get("", response_model=LotListOut)
async def list_lots(
    karat: str = "",
    source: str = "",
    include_depleted: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    q = select(GoldLot)
    if karat:
        q = q.where(GoldLot.karat == Karat(karat))
    if source:
        q = q.where(GoldLot.source == LotSource(source))
    if not include_depleted:
        q = q.where(GoldLot.is_depleted.is_(False))

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()

    q = q.order_by(GoldLot.acquired_at.desc()).offset((page - 1) * page_size).limit(page_size)
    lots = (await db.execute(q)).scalars().all()

    return LotListOut(
        items=[LotOut.model_validate(l) for l in lots],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/totals", response_model=LotTotalsOut)
async def lot_totals(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Per-karat pool totals across non-depleted lots."""
    stmt = (
        select(
            GoldLot.karat,
            func.coalesce(func.sum(GoldLot.weight_remaining_grams), 0).label("remaining"),
            func.coalesce(func.sum(GoldLot.weight_grams), 0).label("original"),
            func.count(GoldLot.id).label("lot_count"),
            func.coalesce(
                func.sum(
                    GoldLot.cost_basis_usd
                    * GoldLot.weight_remaining_grams
                    / func.nullif(GoldLot.weight_grams, 0)
                ),
                0,
            ).label("cost_remaining"),
        )
        .where(GoldLot.is_depleted.is_(False))
        .group_by(GoldLot.karat)
    )
    rows = (await db.execute(stmt)).all()

    by_karat = [
        LotKaratTotal(
            karat=r.karat.value if hasattr(r.karat, "value") else str(r.karat),
            total_remaining_grams=Decimal(str(r.remaining)),
            total_original_grams=Decimal(str(r.original)),
            lot_count=int(r.lot_count),
            cost_basis_remaining_usd=Decimal(str(r.cost_remaining)).quantize(Decimal("0.01")),
        )
        for r in rows
    ]
    grand = sum((k.total_remaining_grams for k in by_karat), Decimal("0"))
    return LotTotalsOut(by_karat=by_karat, grand_total_remaining_grams=grand)


@router.post("", response_model=LotOut, status_code=201)
async def create_lot(
    body: LotCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    try:
        karat_val = Karat(body.karat)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid karat '{body.karat}'")
    try:
        source_val = LotSource(body.source)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid source '{body.source}'")
    if source_val not in _API_CREATABLE_SOURCES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Source '{body.source}' cannot be created via this endpoint; "
                "use the buyback / supplier purchase / melt endpoints (Phases 3–6)."
            ),
        )

    lot = GoldLot(
        karat=karat_val,
        weight_grams=body.weight_grams,
        weight_remaining_grams=body.weight_grams,
        source=source_val,
        cost_basis_usd=body.cost_basis_usd,
        notes=body.notes,
    )
    if body.acquired_at is not None:
        lot.acquired_at = body.acquired_at
    db.add(lot)
    await db.flush()

    await record(
        db,
        event_type=EVENT_LOT_CREATED,
        actor_user_id=user.id,
        ref_type="gold_lot",
        ref_id=lot.id,
        payload={
            "karat": karat_val.value,
            "weight_grams": str(body.weight_grams),
            "source": source_val.value,
            "cost_basis_usd": str(body.cost_basis_usd),
        },
    )

    await db.commit()
    await db.refresh(lot)
    return LotOut.model_validate(lot)


@router.get("/{lot_id}", response_model=LotOut)
async def get_lot(
    lot_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    lot = (await db.execute(select(GoldLot).where(GoldLot.id == lot_id))).scalar_one_or_none()
    if not lot:
        raise HTTPException(status_code=404, detail="Lot not found")
    return LotOut.model_validate(lot)


@router.patch("/{lot_id}", response_model=LotOut)
async def update_lot(
    lot_id: str,
    body: LotUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    lot = (await db.execute(select(GoldLot).where(GoldLot.id == lot_id))).scalar_one_or_none()
    if not lot:
        raise HTTPException(status_code=404, detail="Lot not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(lot, field, value)
    await db.commit()
    await db.refresh(lot)
    return LotOut.model_validate(lot)
