"""Inventory ledger helper.

Every state mutation in the inventory layer writes one row here, inside the
same DB transaction as the state change. All writes go through `record()`.

AUDIT RATIONALE (phase A1)
--------------------------
Each appended row is hash-chained to the previous one. `record()`:
  1. SELECT ... FOR UPDATE locks the singleton `inventory_ledger_chain_head`
     row so concurrent appends serialize on the same lock (works in Postgres
     and in the SQLite test fixture; no PG-specific advisory locks needed).
  2. Computes `entry_hash = sha256(canonical(payload+meta) || prev_hash)` via
     the pure helper in `app.core.audit_chain`.
  3. Inserts the ledger row with both `prev_hash` and `entry_hash` populated.
  4. Advances the head row's `latest_entry_hash` and `row_count`.

All four steps live in the caller's transaction — the head update commits
atomically with the ledger row and the underlying state change. If the
caller rolls back, the head row's value also rolls back, preserving chain
integrity.

The ledger is also append-only at the API layer (no UPDATE/DELETE endpoints
exist) and database grants are tightened in audit phase A2. Tamper detection
is exposed via `GET /api/ledger/verify` (audit phase A1.2).

Event type names are documentation-style strings, not a postgres enum, so new
phases can introduce new event types without a schema migration.
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_chain import compute_ledger_entry_hash
from app.models import InventoryLedger, InventoryLedgerChainHead


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
# Phase 1 — per-item (partial) refunds
EVENT_ORDER_ITEM_REFUND = "ORDER_ITEM_REFUND"

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

# Accounting (GL Core, Module 0) — every GL post writes one of these into the
# InventoryLedger in the same transaction (design §3.1 same-transaction audit).
EVENT_GL_ENTRY_POSTED = "GL_ENTRY_POSTED"
EVENT_GL_ENTRY_REVERSED = "GL_ENTRY_REVERSED"
EVENT_GL_PERIOD_CLOSED = "GL_PERIOD_CLOSED"
EVENT_GL_PERIOD_REOPENED = "GL_PERIOD_REOPENED"
EVENT_GL_YEAR_CLOSED = "GL_YEAR_CLOSED"
EVENT_GL_ACCOUNT_CREATED = "GL_ACCOUNT_CREATED"
EVENT_GL_ACCOUNT_UPDATED = "GL_ACCOUNT_UPDATED"

# Audit phase A3 — sensitive admin actions that previously wrote no ledger row.
EVENT_SETTINGS_CHANGED = "SETTINGS_CHANGED"
EVENT_GOLD_RATE_OVERRIDE_SET = "GOLD_RATE_OVERRIDE_SET"
EVENT_GOLD_RATE_OVERRIDE_CLEARED = "GOLD_RATE_OVERRIDE_CLEARED"
EVENT_GOLD_RATE_REFRESH_TRIGGERED = "GOLD_RATE_REFRESH_TRIGGERED"
EVENT_STAFF_CREATED = "STAFF_CREATED"
EVENT_STAFF_UPDATED = "STAFF_UPDATED"

# Audit phase B2 — stock-take workflow events. These are the workflow
# wrappers; APPROVE additionally emits a COIN_STOCK_ADJUSTED or
# OUNCE_STOCK_ADJUSTED event via apply_unit_stock_adjustment_core, with
# stock_take_line_id in the payload for cross-reference.
EVENT_STOCK_TAKE_STARTED = "STOCK_TAKE_STARTED"
EVENT_STOCK_TAKE_SUBMITTED = "STOCK_TAKE_SUBMITTED"
EVENT_STOCK_TAKE_LINE_APPROVED = "STOCK_TAKE_LINE_APPROVED"
EVENT_STOCK_TAKE_LINE_REJECTED = "STOCK_TAKE_LINE_REJECTED"
EVENT_STOCK_TAKE_CLOSED = "STOCK_TAKE_CLOSED"

# Admin catalog mutations (2026-06-15)
EVENT_CATEGORY_CREATED = "CATEGORY_CREATED"
EVENT_CATEGORY_UPDATED = "CATEGORY_UPDATED"
EVENT_CATEGORY_DELETED = "CATEGORY_DELETED"
EVENT_PRODUCT_CREATED = "PRODUCT_CREATED"
EVENT_PRODUCT_UPDATED = "PRODUCT_UPDATED"
EVENT_PRODUCT_DELETED = "PRODUCT_DELETED"


def field_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return only the fields whose value changed, as {key: {from, to}}.

    Used by audit-trail callers that record SETTINGS_CHANGED / STAFF_UPDATED
    events so the ledger payload contains exactly what an auditor needs to
    answer "what changed, and from what to what?" Unchanged fields are
    omitted to keep the payload small and readable.

    Decimals/datetimes are stringified so the payload survives JSON
    serialization without precision loss.
    """
    def _normalize(v: Any) -> Any:
        if hasattr(v, "isoformat"):
            return v.isoformat()
        # Decimal, UUID, anything else with a stable str() form
        if not isinstance(v, (str, int, float, bool, type(None), list, dict)):
            return str(v)
        return v

    out: dict[str, dict[str, Any]] = {}
    for key in set(before) | set(after):
        b = before.get(key)
        a = after.get(key)
        if b != a:
            out[key] = {"from": _normalize(b), "to": _normalize(a)}
    return out


async def record(
    db: AsyncSession,
    *,
    event_type: str,
    actor_user_id: str,
    ref_type: str,
    ref_id: str,
    payload: dict[str, Any] | None = None,
) -> InventoryLedger:
    """Append a hash-chained ledger row. Does NOT commit — caller owns the
    transaction.

    AUDIT: see module docstring. The chain-head row is locked FOR UPDATE so
    concurrent appends from other transactions block here until this one
    commits or rolls back, preventing two writers from producing two children
    of the same parent (which would fork the chain).
    """
    # 1. Lock the head row. SELECT ... FOR UPDATE on a single-row table is
    #    the simplest serialization primitive that works on both PG and the
    #    SQLite test fixture (where it's a no-op but writes serialize anyway).
    head = (
        await db.execute(
            select(InventoryLedgerChainHead)
            .where(InventoryLedgerChainHead.id == 1)
            .with_for_update()
        )
    ).scalar_one()

    # 2. Set the timestamp now so it's part of the hash. Without this, the
    #    DB server_default would set it on INSERT, but at that point the
    #    hash has already been computed without knowing the exact value.
    occurred_at = datetime.now(timezone.utc)
    payload_dict = payload or {}

    # 3. Compute the chain hash over the semantic event.
    entry_hash = compute_ledger_entry_hash(
        prev_hash=head.latest_entry_hash,
        fields={
            "event_type": event_type,
            "actor_user_id": actor_user_id,
            "occurred_at": occurred_at,
            "ref_type": ref_type,
            "ref_id": ref_id,
            "payload": payload_dict,
        },
    )

    # 4. Persist the row with chain pointers set.
    entry = InventoryLedger(
        event_type=event_type,
        actor_user_id=actor_user_id,
        occurred_at=occurred_at,
        ref_type=ref_type,
        ref_id=ref_id,
        payload=payload_dict,
        prev_hash=head.latest_entry_hash,
        entry_hash=entry_hash,
    )
    db.add(entry)

    # 5. Advance the head. Both writes flush together; both commit (or roll
    #    back) atomically with the caller's transaction.
    head.latest_entry_hash = entry_hash
    head.row_count = head.row_count + 1
    await db.flush()
    return entry
