from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.gold_api import get_current_gold_rate
from app.core.ledger import EVENT_COIN_TYPE_CREATED, EVENT_COIN_TYPE_UPDATED, record
from app.core.permissions import require_admin
from app.core.pricing import calculate_unit_price, generate_unit_code
from app.deps import get_current_user, get_db
from app.models import CoinType, Karat, MarginMode, User
from app.schemas.unit_stock import (
    UnitPriceOut, UnitTypeCreate, UnitTypeListOut, UnitTypeOut, UnitTypeUpdate,
)

router = APIRouter(prefix="/coins", tags=["coins"])


def _validate_karat(value: str) -> Karat:
    try:
        return Karat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid karat '{value}'")


def _validate_margin_mode(value: str) -> MarginMode:
    try:
        return MarginMode(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid margin_mode '{value}'")


@router.get("", response_model=UnitTypeListOut)
async def list_coin_types(
    search: str = "",
    is_active: bool | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = select(CoinType)
    if search:
        q = q.where(CoinType.code.ilike(f"%{search}%") | CoinType.name_en.ilike(f"%{search}%"))
    if is_active is not None:
        q = q.where(CoinType.is_active.is_(is_active))

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    q = q.order_by(CoinType.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(q)).scalars().all()
    return UnitTypeListOut(
        items=[UnitTypeOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("", response_model=UnitTypeOut, status_code=201)
async def create_coin_type(
    body: UnitTypeCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    karat = _validate_karat(body.karat)
    margin_mode = _validate_margin_mode(body.margin_mode)

    code = body.code or await generate_unit_code(db, "COIN", karat)
    existing = (await db.execute(select(CoinType).where(CoinType.code == code))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail=f"Coin type with code '{code}' already exists")

    coin = CoinType(
        code=code,
        name_en=body.name_en,
        name_ar=body.name_ar,
        karat=karat,
        weight_grams=body.weight_grams,
        markup_per_gram=body.markup_per_gram,
        margin_mode=margin_mode,
        margin_value=body.margin_value,
        min_stock_qty=body.min_stock_qty,
        photo_url=body.photo_url,
    )
    db.add(coin)
    await db.flush()
    await record(
        db,
        event_type=EVENT_COIN_TYPE_CREATED,
        actor_user_id=user.id,
        ref_type="coin_type",
        ref_id=coin.id,
        payload={
            "code": coin.code,
            "karat": karat.value,
            "weight_grams": str(coin.weight_grams),
            "markup_per_gram": str(coin.markup_per_gram),
            "margin_mode": margin_mode.value,
            "margin_value": str(coin.margin_value),
        },
    )
    await db.commit()
    await db.refresh(coin)
    return UnitTypeOut.model_validate(coin)


@router.get("/{coin_id}", response_model=UnitTypeOut)
async def get_coin_type(
    coin_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    coin = (await db.execute(select(CoinType).where(CoinType.id == coin_id))).scalar_one_or_none()
    if not coin:
        raise HTTPException(status_code=404, detail="Coin type not found")
    return UnitTypeOut.model_validate(coin)


@router.get("/{coin_id}/price", response_model=UnitPriceOut)
async def coin_price(
    coin_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    coin = (await db.execute(select(CoinType).where(CoinType.id == coin_id))).scalar_one_or_none()
    if not coin:
        raise HTTPException(status_code=404, detail="Coin type not found")

    rate_info = await get_current_gold_rate(db)
    rate_24k = Decimal(str(rate_info["rate"]))
    priced = calculate_unit_price(
        rate_24k=rate_24k,
        weight_grams=coin.weight_grams,
        markup_per_gram=coin.markup_per_gram,
        margin_mode=coin.margin_mode.value,
        margin_value=coin.margin_value,
    )
    return UnitPriceOut(
        type_id=coin.id,
        code=coin.code,
        gold_rate_24k=float(rate_24k),
        effective_rate=priced["effective_rate"],
        metal_value=priced["metal_value"],
        margin_amount=priced["margin_amount"],
        final_price=priced["final_price"],
        on_hand_qty=coin.on_hand_qty,
        rate_source=rate_info["source"],
        rate_is_stale=bool(rate_info.get("is_stale", False)),
    )


@router.patch("/{coin_id}", response_model=UnitTypeOut)
async def update_coin_type(
    coin_id: str,
    body: UnitTypeUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    coin = (await db.execute(select(CoinType).where(CoinType.id == coin_id))).scalar_one_or_none()
    if not coin:
        raise HTTPException(status_code=404, detail="Coin type not found")

    updates = body.model_dump(exclude_unset=True)
    if "karat" in updates and updates["karat"] is not None:
        updates["karat"] = _validate_karat(updates["karat"])
    if "margin_mode" in updates and updates["margin_mode"] is not None:
        updates["margin_mode"] = _validate_margin_mode(updates["margin_mode"])

    for field, value in updates.items():
        setattr(coin, field, value)
    await db.flush()
    await record(
        db,
        event_type=EVENT_COIN_TYPE_UPDATED,
        actor_user_id=user.id,
        ref_type="coin_type",
        ref_id=coin.id,
        payload={"changed": {k: str(v) for k, v in updates.items()}},
    )
    await db.commit()
    await db.refresh(coin)
    return UnitTypeOut.model_validate(coin)


@router.delete("/{coin_id}", status_code=204)
async def delete_coin_type(
    coin_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    coin = (await db.execute(select(CoinType).where(CoinType.id == coin_id))).scalar_one_or_none()
    if not coin:
        raise HTTPException(status_code=404, detail="Coin type not found")
    coin.is_active = False
    await db.flush()
    await record(
        db,
        event_type=EVENT_COIN_TYPE_UPDATED,
        actor_user_id=user.id,
        ref_type="coin_type",
        ref_id=coin.id,
        payload={"changed": {"is_active": False}, "soft_delete": True},
    )
    await db.commit()
