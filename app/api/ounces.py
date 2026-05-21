from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.gold_api import get_current_gold_rate
from app.core.ledger import EVENT_OUNCE_TYPE_CREATED, EVENT_OUNCE_TYPE_UPDATED, record
from app.core.permissions import require_admin
from app.core.pricing import calculate_unit_price, generate_unit_code
from app.deps import get_db
from app.models import Karat, MarginMode, OunceType, User
from app.schemas.unit_stock import (
    UnitPriceOut, UnitTypeCreate, UnitTypeListOut, UnitTypeOut, UnitTypeUpdate,
)

router = APIRouter(prefix="/ounces", tags=["ounces"])


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
async def list_ounce_types(
    search: str = "",
    is_active: bool | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    q = select(OunceType)
    if search:
        q = q.where(OunceType.code.ilike(f"%{search}%") | OunceType.name_en.ilike(f"%{search}%"))
    if is_active is not None:
        q = q.where(OunceType.is_active.is_(is_active))

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    q = q.order_by(OunceType.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(q)).scalars().all()
    return UnitTypeListOut(
        items=[UnitTypeOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("", response_model=UnitTypeOut, status_code=201)
async def create_ounce_type(
    body: UnitTypeCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    karat = _validate_karat(body.karat)
    margin_mode = _validate_margin_mode(body.margin_mode)

    code = body.code or await generate_unit_code(db, "OUNCE", karat)
    existing = (await db.execute(select(OunceType).where(OunceType.code == code))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail=f"Ounce type with code '{code}' already exists")

    bar = OunceType(
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
    db.add(bar)
    await db.flush()
    await record(
        db,
        event_type=EVENT_OUNCE_TYPE_CREATED,
        actor_user_id=user.id,
        ref_type="ounce_type",
        ref_id=bar.id,
        payload={
            "code": bar.code,
            "karat": karat.value,
            "weight_grams": str(bar.weight_grams),
            "markup_per_gram": str(bar.markup_per_gram),
            "margin_mode": margin_mode.value,
            "margin_value": str(bar.margin_value),
        },
    )
    await db.commit()
    await db.refresh(bar)
    return UnitTypeOut.model_validate(bar)


@router.get("/{ounce_id}", response_model=UnitTypeOut)
async def get_ounce_type(
    ounce_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    bar = (await db.execute(select(OunceType).where(OunceType.id == ounce_id))).scalar_one_or_none()
    if not bar:
        raise HTTPException(status_code=404, detail="Ounce type not found")
    return UnitTypeOut.model_validate(bar)


@router.get("/{ounce_id}/price", response_model=UnitPriceOut)
async def ounce_price(
    ounce_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    bar = (await db.execute(select(OunceType).where(OunceType.id == ounce_id))).scalar_one_or_none()
    if not bar:
        raise HTTPException(status_code=404, detail="Ounce type not found")

    rate_info = await get_current_gold_rate(db)
    rate_24k = Decimal(str(rate_info["rate"]))
    priced = calculate_unit_price(
        rate_24k=rate_24k,
        weight_grams=bar.weight_grams,
        markup_per_gram=bar.markup_per_gram,
        margin_mode=bar.margin_mode.value,
        margin_value=bar.margin_value,
    )
    return UnitPriceOut(
        type_id=bar.id,
        code=bar.code,
        gold_rate_24k=float(rate_24k),
        effective_rate=priced["effective_rate"],
        metal_value=priced["metal_value"],
        margin_amount=priced["margin_amount"],
        final_price=priced["final_price"],
        on_hand_qty=bar.on_hand_qty,
        rate_source=rate_info["source"],
        rate_is_stale=bool(rate_info.get("is_stale", False)),
    )


@router.patch("/{ounce_id}", response_model=UnitTypeOut)
async def update_ounce_type(
    ounce_id: str,
    body: UnitTypeUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    bar = (await db.execute(select(OunceType).where(OunceType.id == ounce_id))).scalar_one_or_none()
    if not bar:
        raise HTTPException(status_code=404, detail="Ounce type not found")

    updates = body.model_dump(exclude_unset=True)
    if "karat" in updates and updates["karat"] is not None:
        updates["karat"] = _validate_karat(updates["karat"])
    if "margin_mode" in updates and updates["margin_mode"] is not None:
        updates["margin_mode"] = _validate_margin_mode(updates["margin_mode"])

    for field, value in updates.items():
        setattr(bar, field, value)
    await db.flush()
    await record(
        db,
        event_type=EVENT_OUNCE_TYPE_UPDATED,
        actor_user_id=user.id,
        ref_type="ounce_type",
        ref_id=bar.id,
        payload={"changed": {k: str(v) for k, v in updates.items()}},
    )
    await db.commit()
    await db.refresh(bar)
    return UnitTypeOut.model_validate(bar)


@router.delete("/{ounce_id}", status_code=204)
async def delete_ounce_type(
    ounce_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    bar = (await db.execute(select(OunceType).where(OunceType.id == ounce_id))).scalar_one_or_none()
    if not bar:
        raise HTTPException(status_code=404, detail="Ounce type not found")
    bar.is_active = False
    await db.flush()
    await record(
        db,
        event_type=EVENT_OUNCE_TYPE_UPDATED,
        actor_user_id=user.id,
        ref_type="ounce_type",
        ref_id=bar.id,
        payload={"changed": {"is_active": False}, "soft_delete": True},
    )
    await db.commit()
