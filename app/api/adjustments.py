from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ledger import (
    EVENT_COIN_STOCK_ADJUSTED,
    EVENT_MANUAL_ADJUSTMENT,
    EVENT_OUNCE_STOCK_ADJUSTED,
    EVENT_PRODUCT_STATUS_CHANGED,
    record,
)
from app.core.permissions import require_admin
from app.deps import get_db
from app.models import (
    AdjustmentReason, AdjustmentTarget, CoinType, GoldLot, ManualAdjustment,
    OunceType, Product, ProductStatus, User,
)
from app.schemas.adjustment import AdjustmentCreate, AdjustmentOut

router = APIRouter(prefix="/adjustments", tags=["adjustments"])


_SUPPORTED_TARGETS = {
    AdjustmentTarget.LOT,
    AdjustmentTarget.COIN_STOCK,
    AdjustmentTarget.OUNCE_STOCK,
    AdjustmentTarget.PRODUCT,
}


@router.post("", response_model=AdjustmentOut, status_code=201)
async def create_adjustment(
    body: AdjustmentCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    try:
        target_type = AdjustmentTarget(body.target_type)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid target_type '{body.target_type}'")
    try:
        reason = AdjustmentReason(body.reason)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid reason '{body.reason}'")

    if target_type not in _SUPPORTED_TARGETS:
        raise HTTPException(
            status_code=422,
            detail=f"Adjustments for target '{target_type.value}' are not enabled yet.",
        )
    if body.delta == 0:
        raise HTTPException(status_code=422, detail="delta must be non-zero")

    if target_type in (AdjustmentTarget.COIN_STOCK, AdjustmentTarget.OUNCE_STOCK):
        return await _adjust_unit_stock(db, body, user, target_type, reason)

    if target_type == AdjustmentTarget.PRODUCT:
        return await _adjust_product(db, body, user, reason)

    if target_type == AdjustmentTarget.LOT:
        lot = (
            await db.execute(
                select(GoldLot).where(GoldLot.id == body.target_id).with_for_update()
            )
        ).scalar_one_or_none()
        if not lot:
            raise HTTPException(status_code=404, detail=f"Lot {body.target_id} not found")

        new_remaining = lot.weight_remaining_grams + body.delta
        if new_remaining < 0:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Adjustment would push lot remaining below zero: "
                    f"current {lot.weight_remaining_grams}, delta {body.delta}"
                ),
            )

        prev_remaining = lot.weight_remaining_grams
        lot.weight_remaining_grams = new_remaining
        lot.is_depleted = new_remaining <= Decimal("0")

        adj = ManualAdjustment(
            target_type=target_type,
            target_id=body.target_id,
            delta=body.delta,
            reason=reason,
            notes=body.notes,
            actor_user_id=user.id,
        )
        db.add(adj)
        await db.flush()

        await record(
            db,
            event_type=EVENT_MANUAL_ADJUSTMENT,
            actor_user_id=user.id,
            ref_type="gold_lot",
            ref_id=lot.id,
            payload={
                "adjustment_id": adj.id,
                "delta_grams": str(body.delta),
                "reason": reason.value,
                "notes": body.notes,
                "remaining_before": str(prev_remaining),
                "remaining_after": str(new_remaining),
                "is_depleted_after": lot.is_depleted,
            },
        )
        await db.commit()
        await db.refresh(adj)
        return AdjustmentOut.model_validate(adj)

    # Unreachable — earlier guard catches non-LOT targets.
    raise HTTPException(status_code=500, detail="unhandled target_type")


async def _adjust_product(
    db,
    body: AdjustmentCreate,
    user: User,
    reason: AdjustmentReason,
) -> AdjustmentOut:
    """Mark a product as out-of-stock (delta=-1) or restore it (delta=+1).

    Atomic products don't have a quantity; "adjusting" them means flipping the
    status. delta=-1 sets status=INACTIVE (LOSS/THEFT/GIFT/SAMPLE) or applies a
    CORRECTION. delta=+1 restores AVAILABLE if currently INACTIVE.
    """
    if body.delta not in (Decimal("-1"), Decimal("1")):
        raise HTTPException(
            status_code=422,
            detail="PRODUCT adjustment delta must be -1 (remove) or +1 (restore)",
        )

    product = (
        await db.execute(
            select(Product).where(Product.id == body.target_id).with_for_update()
        )
    ).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail=f"Product {body.target_id} not found")

    prev_status = product.status
    if body.delta == Decimal("-1"):
        if product.status in (ProductStatus.SOLD, ProductStatus.MELTED):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot remove {product.code} from stock — already {product.status.value}. "
                    "Sold/melted products are immutable."
                ),
            )
        product.status = ProductStatus.INACTIVE
    else:  # delta == +1
        if product.status != ProductStatus.INACTIVE:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Can only restore products currently INACTIVE; "
                    f"{product.code} is {product.status.value}."
                ),
            )
        product.status = ProductStatus.AVAILABLE

    adj = ManualAdjustment(
        target_type=AdjustmentTarget.PRODUCT,
        target_id=body.target_id,
        delta=body.delta,
        reason=reason,
        notes=body.notes,
        actor_user_id=user.id,
    )
    db.add(adj)
    await db.flush()

    await record(
        db,
        event_type=EVENT_PRODUCT_STATUS_CHANGED,
        actor_user_id=user.id,
        ref_type="product",
        ref_id=product.id,
        payload={
            "adjustment_id": adj.id,
            "product_code": product.code,
            "reason": reason.value,
            "notes": body.notes,
            "status_before": prev_status.value,
            "status_after": product.status.value,
        },
    )
    await db.commit()
    await db.refresh(adj)
    return AdjustmentOut.model_validate(adj)


async def _adjust_unit_stock(
    db,
    body: AdjustmentCreate,
    user: User,
    target_type: AdjustmentTarget,
    reason: AdjustmentReason,
) -> AdjustmentOut:
    """Apply a quantity delta to a coin_types.on_hand_qty or ounce_types row."""
    # Quantity is integer; reject fractional deltas.
    if body.delta != body.delta.to_integral_value():
        raise HTTPException(
            status_code=422,
            detail="delta must be an integer for COIN_STOCK / OUNCE_STOCK adjustments",
        )
    delta_int = int(body.delta)

    if target_type == AdjustmentTarget.COIN_STOCK:
        Model = CoinType
        event = EVENT_COIN_STOCK_ADJUSTED
        ref_kind = "coin_type"
    else:
        Model = OunceType
        event = EVENT_OUNCE_STOCK_ADJUSTED
        ref_kind = "ounce_type"

    row = (
        await db.execute(
            select(Model).where(Model.id == body.target_id).with_for_update()
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail=f"{ref_kind} {body.target_id} not found")

    new_qty = row.on_hand_qty + delta_int
    if new_qty < 0:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Adjustment would push on_hand_qty below zero: "
                f"current {row.on_hand_qty}, delta {delta_int}"
            ),
        )

    prev_qty = row.on_hand_qty
    row.on_hand_qty = new_qty

    adj = ManualAdjustment(
        target_type=target_type,
        target_id=body.target_id,
        delta=body.delta,
        reason=reason,
        notes=body.notes,
        actor_user_id=user.id,
    )
    db.add(adj)
    await db.flush()

    await record(
        db,
        event_type=event,
        actor_user_id=user.id,
        ref_type=ref_kind,
        ref_id=row.id,
        payload={
            "adjustment_id": adj.id,
            "delta_qty": delta_int,
            "reason": reason.value,
            "notes": body.notes,
            "qty_before": prev_qty,
            "qty_after": new_qty,
        },
    )
    await db.commit()
    await db.refresh(adj)
    return AdjustmentOut.model_validate(adj)


@router.get("", response_model=list[AdjustmentOut])
async def list_adjustments(
    target_type: str = "",
    target_id: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    q = select(ManualAdjustment)
    if target_type:
        try:
            q = q.where(ManualAdjustment.target_type == AdjustmentTarget(target_type))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid target_type '{target_type}'")
    if target_id:
        q = q.where(ManualAdjustment.target_id == target_id)
    q = q.order_by(ManualAdjustment.occurred_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(q)).scalars().all()
    return [AdjustmentOut.model_validate(r) for r in rows]
