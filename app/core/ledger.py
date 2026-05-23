"""Inventory ledger helper.

Every state mutation in the inventory layer writes one row here, inside the
same DB transaction as the state change. The ledger is append-only at the API
layer (no UPDATE/DELETE endpoints exist). All writes go through `record()`.

Event type names are documentation-style strings, not a postgres enum, so new
phases can introduce new event types without a schema migration.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import InventoryLedger


# Phase 1 event types. Future phases add: BUYBACK_*, SUPPLIER_*, SALE_*,
# MELT, POLISH, ORDER_VOID, etc.
EVENT_LOT_CREATED = "LOT_CREATED"
EVENT_LOT_CONSUMED = "LOT_CONSUMED"
EVENT_LOT_DEPLETED = "LOT_DEPLETED"
EVENT_MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT"

# Phase 3 — walk-in buybacks
EVENT_BUYBACK_PURE_GOLD = "BUYBACK_PURE_GOLD"
EVENT_BUYBACK_COIN = "BUYBACK_COIN"
EVENT_BUYBACK_OUNCE = "BUYBACK_OUNCE"
EVENT_BUYBACK_USED_PRODUCT = "BUYBACK_USED_PRODUCT"

# Phase 4 — sales + voids + product stock
EVENT_SALE_PRODUCT = "SALE_PRODUCT"
EVENT_SALE_COIN = "SALE_COIN"
EVENT_SALE_OUNCE = "SALE_OUNCE"
EVENT_ORDER_VOID = "ORDER_VOID"
EVENT_PRODUCT_STATUS_CHANGED = "PRODUCT_STATUS_CHANGED"

# Phase 6 — melt + polish
EVENT_MELT = "MELT"
EVENT_POLISH = "POLISH"

# Phase 5 — suppliers + procurement + AP
EVENT_SUPPLIER_CREATED = "SUPPLIER_CREATED"
EVENT_SUPPLIER_UPDATED = "SUPPLIER_UPDATED"
EVENT_SUPPLIER_PURCHASE = "SUPPLIER_PURCHASE"
EVENT_SUPPLIER_PAYMENT_CASH = "SUPPLIER_PAYMENT_CASH"
EVENT_SUPPLIER_PAYMENT_GOLD = "SUPPLIER_PAYMENT_GOLD"
EVENT_SUPPLIER_BALANCE_CHANGED = "SUPPLIER_BALANCE_CHANGED"

# Phase 2
EVENT_COIN_TYPE_CREATED = "COIN_TYPE_CREATED"
EVENT_COIN_TYPE_UPDATED = "COIN_TYPE_UPDATED"
EVENT_COIN_STOCK_ADJUSTED = "COIN_STOCK_ADJUSTED"
EVENT_OUNCE_TYPE_CREATED = "OUNCE_TYPE_CREATED"
EVENT_OUNCE_TYPE_UPDATED = "OUNCE_TYPE_UPDATED"
EVENT_OUNCE_STOCK_ADJUSTED = "OUNCE_STOCK_ADJUSTED"

# Zakat
EVENT_ZAKAT_SNAPSHOT_CREATED = "ZAKAT_SNAPSHOT_CREATED"


async def record(
    db: AsyncSession,
    *,
    event_type: str,
    actor_user_id: str,
    ref_type: str,
    ref_id: str,
    payload: dict[str, Any] | None = None,
) -> InventoryLedger:
    """Append a ledger row. Does NOT commit — caller owns the transaction."""
    entry = InventoryLedger(
        event_type=event_type,
        actor_user_id=actor_user_id,
        ref_type=ref_type,
        ref_id=ref_id,
        payload=payload or {},
    )
    db.add(entry)
    await db.flush()
    return entry
