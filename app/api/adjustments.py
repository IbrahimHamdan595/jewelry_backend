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


async def apply_unit_stock_adjustment_core(
    db: AsyncSession,
    *,
    target_type: AdjustmentTarget,
    target_id: str,
    delta: Decimal,
    reason: AdjustmentReason,
    notes: str | None,
    actor_user_id: str,
    ledger_extra: dict | None = None,
) -> ManualAdjustment:
    """The ONLY code path that mutates CoinType.on_hand_qty / OunceType.on_hand_qty.

    AUDIT (B2): the HTTP POST /adjustments handler calls this; the stock-take
    approval endpoint calls this. ANY future feature that needs to change
    coin/ounce stock MUST go through here. A code-review red flag if you
    see another path directly assigning to `on_hand_qty`.

    Behavior:
      • Locks the target row with FOR UPDATE.
      • Validates delta is integer and resulting qty >= 0 (raises 422 otherwise).
      • Mutates on_hand_qty.
      • Inserts a ManualAdjustment row.
      • Writes a COIN_STOCK_ADJUSTED / OUNCE_STOCK_ADJUSTED chained ledger
        event. `ledger_extra` is merged into the payload — used by stock-take
        approval to record `stock_take_line_id` for cross-reference.
      • Does NOT commit. Caller owns the transaction so the audit row, the
        on_hand_qty change, and any sibling workflow rows all commit together
        (or roll back together).

    Returns the ManualAdjustment row. Caller may want to flush + refresh
    before using FK fields.
    """
    if target_type not in (AdjustmentTarget.COIN_STOCK, AdjustmentTarget.OUNCE_STOCK):
        raise HTTPException(
            status_code=422,
            detail=(
                f"apply_unit_stock_adjustment_core only handles COIN_STOCK / "
                f"OUNCE_STOCK; got {target_type!r}."
            ),
        )

    if delta != delta.to_integral_value():
        raise HTTPException(
            status_code=422,
            detail="delta must be an integer for COIN_STOCK / OUNCE_STOCK adjustments",
        )
    delta_int = int(delta)

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
            select(Model).where(Model.id == target_id).with_for_update()
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail=f"{ref_kind} {target_id} not found")

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
        target_id=target_id,
        delta=delta,
        reason=reason,
        notes=notes,
        actor_user_id=actor_user_id,
    )
    db.add(adj)
    await db.flush()

    payload: dict = {
        "adjustment_id": adj.id,
        "delta_qty": delta_int,
        "reason": reason.value,
        "notes": notes,
        "qty_before": prev_qty,
        "qty_after": new_qty,
    }
    if ledger_extra:
        payload.update(ledger_extra)

    await record(
        db,
        event_type=event,
        actor_user_id=actor_user_id,
        ref_type=ref_kind,
        ref_id=row.id,
        payload=payload,
    )
    return adj


async def _adjust_unit_stock(
    db,
    body: AdjustmentCreate,
    user: User,
    target_type: AdjustmentTarget,
    reason: AdjustmentReason,
) -> AdjustmentOut:
    """HTTP-handler thin wrapper around `apply_unit_stock_adjustment_core`."""
    adj = await apply_unit_stock_adjustment_core(
        db,
        target_type=target_type,
        target_id=body.target_id,
        delta=body.delta,
        reason=reason,
        notes=body.notes,
        actor_user_id=user.id,
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
