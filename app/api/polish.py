"""Polish endpoint — materializes a USED_PRODUCT buyback as a saleable Product."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ledger import EVENT_POLISH, record
from app.core.permissions import require_admin
from app.core.pricing import generate_item_code
from app.deps import get_db
from app.models import (
    BuybackKind,
    Karat,
    Product,
    ProductStatus,
    User,
    WalkinBuyback,
)
from app.schemas.product import ProductOut
from app.schemas.transitions import PolishCreate, PolishOut

router = APIRouter(prefix="/polish", tags=["polish"])


def _parse_karat(value: str) -> Karat:
    try:
        return Karat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid karat '{value}'")


@router.post("", response_model=PolishOut, status_code=201)
async def create_polish(
    body: PolishCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
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
                f"Only USED_PRODUCT buybacks can be polished; "
                f"this buyback is kind={buyback.kind.value}."
            ),
        )
    if buyback.product_id:
        raise HTTPException(
            status_code=409, detail="This buyback has already been polished",
        )
    if buyback.result_lot_id:
        raise HTTPException(
            status_code=409, detail="This buyback was already melted — cannot polish",
        )
    if buyback.weight_grams is None or buyback.karat is None:
        raise HTTPException(
            status_code=422,
            detail="USED_PRODUCT buyback missing weight or karat — cannot polish",
        )

    karat = _parse_karat(body.override_karat) if body.override_karat else buyback.karat
    weight = (
        body.override_weight_grams
        if body.override_weight_grams is not None
        else buyback.weight_grams
    )

    code = await generate_item_code(db, karat)
    product = Product(
        code=code,
        name_en=body.name_en,
        name_ar=body.name_ar,
        category=body.category,
        category_id=body.category_id,
        karat=karat,
        weight_grams=weight,
        margin_percent=body.margin_percent,
        making_charge=body.making_charge,
        photos=body.photos,
        is_used=True,
        cost_basis_usd=buyback.buy_price_usd,
        status=ProductStatus.AVAILABLE,
        source_ref_type="walkin_buyback",
        source_ref_id=buyback.id,
    )
    db.add(product)
    await db.flush()
    buyback.product_id = product.id

    await record(
        db,
        event_type=EVENT_POLISH,
        actor_user_id=user.id,
        ref_type="walkin_buyback",
        ref_id=buyback.id,
        payload={
            "product_id": product.id,
            "product_code": product.code,
            "karat": karat.value,
            "weight_grams": str(weight),
            "cost_basis_usd": str(buyback.buy_price_usd),
            "seller_name": buyback.seller_name,
            "weight_override": body.override_weight_grams is not None,
            "karat_override": body.override_karat is not None,
            "notes": body.notes,
        },
    )

    await db.commit()
    await db.refresh(product)
    return PolishOut(
        walkin_buyback_id=buyback.id,
        product=ProductOut.model_validate(product),
    )
