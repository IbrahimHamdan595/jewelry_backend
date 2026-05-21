"""Melt endpoint — converts a Product or a USED_PRODUCT buyback into a new gold_lot."""

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ledger import EVENT_LOT_CREATED, EVENT_MELT, record
from app.core.permissions import require_admin
from app.deps import get_db
from app.models import (
    BuybackKind,
    GoldLot,
    Karat,
    LotSource,
    Product,
    ProductStatus,
    User,
    WalkinBuyback,
)
from app.schemas.lot import LotOut
from app.schemas.transitions import MeltCreate, MeltOut

router = APIRouter(prefix="/melts", tags=["melts"])


def _parse_karat(value: str) -> Karat:
    try:
        return Karat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid karat '{value}'")


@router.post("", response_model=MeltOut, status_code=201)
async def create_melt(
    body: MeltCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    if bool(body.product_id) == bool(body.walkin_buyback_id):
        raise HTTPException(
            status_code=422,
            detail="Provide exactly one of product_id or walkin_buyback_id",
        )

    if body.product_id:
        return await _melt_product(db, user, body)
    return await _melt_used_buyback(db, user, body)


async def _melt_product(
    db: AsyncSession, user: User, body: MeltCreate
) -> MeltOut:
    product = (
        await db.execute(
            select(Product).where(Product.id == body.product_id).with_for_update()
        )
    ).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail=f"Product {body.product_id} not found")
    if product.status not in (ProductStatus.AVAILABLE, ProductStatus.INACTIVE):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot melt product {product.code} (status={product.status.value}). "
                "Only AVAILABLE or INACTIVE products can be melted."
            ),
        )

    karat = _parse_karat(body.override_karat) if body.override_karat else product.karat
    weight = body.override_weight_grams if body.override_weight_grams is not None else product.weight_grams
    cost_basis = product.cost_basis_usd if product.cost_basis_usd is not None else Decimal("0")

    lot = GoldLot(
        karat=karat,
        weight_grams=weight,
        weight_remaining_grams=weight,
        source=LotSource.MELT,
        source_ref_type="product",
        source_ref_id=product.id,
        cost_basis_usd=cost_basis,
        notes=body.notes or f"Melted from product {product.code}",
    )
    db.add(lot)
    await db.flush()

    prev_status = product.status
    product.status = ProductStatus.MELTED

    await record(
        db,
        event_type=EVENT_LOT_CREATED,
        actor_user_id=user.id,
        ref_type="gold_lot",
        ref_id=lot.id,
        payload={
            "karat": karat.value,
            "weight_grams": str(weight),
            "source": LotSource.MELT.value,
            "cost_basis_usd": str(cost_basis),
            "from_product_id": product.id,
            "from_product_code": product.code,
        },
    )
    await record(
        db,
        event_type=EVENT_MELT,
        actor_user_id=user.id,
        ref_type="product",
        ref_id=product.id,
        payload={
            "product_code": product.code,
            "status_before": prev_status.value,
            "status_after": ProductStatus.MELTED.value,
            "lot_id": lot.id,
            "karat": karat.value,
            "weight_grams": str(weight),
            "cost_basis_usd": str(cost_basis),
            "weight_override": body.override_weight_grams is not None,
            "karat_override": body.override_karat is not None,
        },
    )

    await db.commit()
    await db.refresh(lot)
    return MeltOut(
        id=lot.id,
        occurred_at=datetime.now(timezone.utc),
        source_type="product",
        source_id=product.id,
        lot=LotOut.model_validate(lot),
    )


async def _melt_used_buyback(
    db: AsyncSession, user: User, body: MeltCreate
) -> MeltOut:
    buyback = (
        await db.execute(
            select(WalkinBuyback)
            .where(WalkinBuyback.id == body.walkin_buyback_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not buyback:
        raise HTTPException(
            status_code=404, detail=f"Buyback {body.walkin_buyback_id} not found"
        )
    if buyback.kind != BuybackKind.USED_PRODUCT:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Only USED_PRODUCT buybacks can be melted from this endpoint; "
                f"this buyback is kind={buyback.kind.value}."
            ),
        )
    # Mutual exclusion: cannot melt if already polished, cannot melt twice.
    if buyback.product_id:
        raise HTTPException(
            status_code=409,
            detail="This buyback was already polished into a product — cannot melt.",
        )
    if buyback.result_lot_id:
        raise HTTPException(
            status_code=409, detail="This buyback was already melted.",
        )

    if buyback.weight_grams is None or buyback.karat is None:
        raise HTTPException(
            status_code=422,
            detail="USED_PRODUCT buyback missing weight or karat — cannot melt",
        )

    karat = _parse_karat(body.override_karat) if body.override_karat else buyback.karat
    weight = (
        body.override_weight_grams
        if body.override_weight_grams is not None
        else buyback.weight_grams
    )
    cost_basis = buyback.buy_price_usd

    lot = GoldLot(
        karat=karat,
        weight_grams=weight,
        weight_remaining_grams=weight,
        source=LotSource.MELT,
        source_ref_type="walkin_buyback",
        source_ref_id=buyback.id,
        cost_basis_usd=cost_basis,
        notes=body.notes or f"Melted from used-product buyback {buyback.id}",
    )
    db.add(lot)
    await db.flush()
    buyback.result_lot_id = lot.id

    await record(
        db,
        event_type=EVENT_LOT_CREATED,
        actor_user_id=user.id,
        ref_type="gold_lot",
        ref_id=lot.id,
        payload={
            "karat": karat.value,
            "weight_grams": str(weight),
            "source": LotSource.MELT.value,
            "cost_basis_usd": str(cost_basis),
            "from_buyback_id": buyback.id,
        },
    )
    await record(
        db,
        event_type=EVENT_MELT,
        actor_user_id=user.id,
        ref_type="walkin_buyback",
        ref_id=buyback.id,
        payload={
            "lot_id": lot.id,
            "karat": karat.value,
            "weight_grams": str(weight),
            "cost_basis_usd": str(cost_basis),
            "seller_name": buyback.seller_name,
            "weight_override": body.override_weight_grams is not None,
            "karat_override": body.override_karat is not None,
        },
    )

    await db.commit()
    await db.refresh(lot)
    return MeltOut(
        id=lot.id,
        occurred_at=datetime.now(timezone.utc),
        source_type="walkin_buyback",
        source_id=buyback.id,
        lot=LotOut.model_validate(lot),
    )
