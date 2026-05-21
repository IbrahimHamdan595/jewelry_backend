"""Internal inventory helpers used across the inventory layer.

`consume_from_lot()` is the only sanctioned way to debit a `GoldLot`. It
locks the row, validates available weight, writes a `GoldLotConsumption`
row, decrements `weight_remaining_grams`, flips `is_depleted` if needed,
and appends a ledger row — all inside the caller's transaction.

Caller owns the commit.
"""

from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ledger import EVENT_LOT_CONSUMED, EVENT_LOT_DEPLETED, record
from app.models import GoldLot, GoldLotConsumption


async def consume_from_lot(
    db: AsyncSession,
    *,
    lot_id: str,
    grams: Decimal,
    ref_type: str,
    ref_id: str,
    actor_user_id: str,
) -> GoldLotConsumption:
    """Debit `grams` from lot `lot_id`. Fails 422 if insufficient."""
    if grams <= 0:
        raise HTTPException(status_code=422, detail="grams must be positive")

    lot = (
        await db.execute(
            select(GoldLot).where(GoldLot.id == lot_id).with_for_update()
        )
    ).scalar_one_or_none()
    if not lot:
        raise HTTPException(status_code=404, detail=f"Lot {lot_id} not found")
    if lot.is_depleted:
        raise HTTPException(status_code=422, detail=f"Lot {lot_id} is depleted")
    if grams > lot.weight_remaining_grams:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Insufficient grams in lot {lot_id}: "
                f"requested {grams}, remaining {lot.weight_remaining_grams}"
            ),
        )

    # Proportional cost basis snapshot from the lot.
    cost_basis_share = (
        (lot.cost_basis_usd * grams / lot.weight_grams).quantize(Decimal("0.01"))
        if lot.weight_grams > 0
        else Decimal("0")
    )

    consumption = GoldLotConsumption(
        lot_id=lot.id,
        grams=grams,
        cost_basis_consumed_usd=cost_basis_share,
        ref_type=ref_type,
        ref_id=ref_id,
        created_by_user_id=actor_user_id,
    )
    db.add(consumption)

    lot.weight_remaining_grams = lot.weight_remaining_grams - grams
    if lot.weight_remaining_grams <= Decimal("0"):
        lot.is_depleted = True

    await db.flush()

    await record(
        db,
        event_type=EVENT_LOT_CONSUMED,
        actor_user_id=actor_user_id,
        ref_type="gold_lot",
        ref_id=lot.id,
        payload={
            "consumption_id": consumption.id,
            "grams": str(grams),
            "cost_basis_consumed_usd": str(cost_basis_share),
            "remaining_after": str(lot.weight_remaining_grams),
            "consumer_ref_type": ref_type,
            "consumer_ref_id": ref_id,
        },
    )
    if lot.is_depleted:
        await record(
            db,
            event_type=EVENT_LOT_DEPLETED,
            actor_user_id=actor_user_id,
            ref_type="gold_lot",
            ref_id=lot.id,
            payload={"depleted_at_consumption_id": consumption.id},
        )

    return consumption
