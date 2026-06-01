"""Phase 7 — inventory alerts + supplier-balance reconciliation.

Audit phase B1 adds `/reconcile-units`: replays every event that mutates
coin/ounce `on_hand_qty` against the stored values, reports drift.
"""

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.notify import send_discord_alert
from app.core.permissions import require_admin
from app.deps import get_db
from app.models import (
    AdjustmentTarget,
    BuybackKind,
    CoinType,
    DebtUnit,
    ManualAdjustment,
    Order,
    OrderItem,
    OrderItemKind,
    OrderStatus,
    OunceType,
    Product,
    ProductStatus,
    Supplier,
    SupplierBalance,
    SupplierItemKind,
    SupplierPayment,
    SupplierPurchase,
    SupplierPurchaseItem,
    User,
    WalkinBuyback,
)

router = APIRouter(prefix="/inventory", tags=["inventory"])


# ── Low-stock alerts ──────────────────────────────────────────────────────────


@router.get("/alerts")
async def inventory_alerts(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Coin, ounce, and product types whose on_hand_qty is at-or-below their
    min_stock_qty.

    Pure-gold lots are intentionally excluded (working pool, not SKUs).
    Products are included as of Phase 3 (they are now stocked-by-quantity);
    only those with a configured min_stock_qty participate.
    """
    coin_rows = (
        await db.execute(
            select(CoinType).where(
                CoinType.is_active.is_(True),
                CoinType.min_stock_qty.is_not(None),
                CoinType.on_hand_qty <= CoinType.min_stock_qty,
            )
        )
    ).scalars().all()
    ounce_rows = (
        await db.execute(
            select(OunceType).where(
                OunceType.is_active.is_(True),
                OunceType.min_stock_qty.is_not(None),
                OunceType.on_hand_qty <= OunceType.min_stock_qty,
            )
        )
    ).scalars().all()
    product_rows = (
        await db.execute(
            select(Product).where(
                Product.is_active.is_(True),
                Product.min_stock_qty.is_not(None),
                Product.on_hand_qty <= Product.min_stock_qty,
                Product.status.notin_((ProductStatus.MELTED, ProductStatus.INACTIVE)),
            )
        )
    ).scalars().all()

    below = []
    for c in coin_rows:
        below.append({
            "kind": "COIN",
            "id": c.id,
            "code": c.code,
            "name_en": c.name_en,
            "on_hand_qty": c.on_hand_qty,
            "min_stock_qty": c.min_stock_qty,
        })
    for o in ounce_rows:
        below.append({
            "kind": "OUNCE",
            "id": o.id,
            "code": o.code,
            "name_en": o.name_en,
            "on_hand_qty": o.on_hand_qty,
            "min_stock_qty": o.min_stock_qty,
        })
    for p in product_rows:
        below.append({
            "kind": "PRODUCT",
            "id": p.id,
            "code": p.code,
            "name_en": p.name_en,
            "on_hand_qty": p.on_hand_qty,
            "min_stock_qty": p.min_stock_qty,
        })
    return {"below_threshold": below, "total": len(below)}


# ── Supplier balance reconciliation ───────────────────────────────────────────


async def _compute_expected_balances(
    db: AsyncSession,
) -> dict[tuple[str, str, str], Decimal]:
    """Replay all supplier_purchases + supplier_payments and return the
    expected (supplier_id, unit, karat) → balance map.

    Karat is "" for CASH rows.
    """
    expected: dict[tuple[str, str, str], Decimal] = {}

    # Aggregate purchases (positive debt creation)
    purchases = (await db.execute(select(SupplierPurchase))).scalars().all()
    for p in purchases:
        cash_due = Decimal(str(p.total_cash_due))
        cash_paid = Decimal(str(p.cash_paid_at_creation))
        cash_owed = cash_due - cash_paid
        if cash_owed != 0:
            key = (p.supplier_id, "CASH", "")
            expected[key] = expected.get(key, Decimal("0")) + cash_owed

        # Gold dues by karat
        for karat_str, due_str in (p.total_grams_due_by_karat or {}).items():
            due = Decimal(str(due_str))
            paid = Decimal(str((p.grams_paid_at_creation_by_karat or {}).get(karat_str, "0")))
            owed = due - paid
            if owed != 0:
                key = (p.supplier_id, "GOLD", karat_str)
                expected[key] = expected.get(key, Decimal("0")) + owed

    # Aggregate payments (reduce debt)
    payments = (await db.execute(select(SupplierPayment))).scalars().all()
    for pay in payments:
        if pay.unit == DebtUnit.CASH:
            key = (pay.supplier_id, "CASH", "")
        else:
            key = (pay.supplier_id, "GOLD", pay.karat.value if pay.karat else "")
        expected[key] = expected.get(key, Decimal("0")) - Decimal(str(pay.amount))

    return expected


@router.get("/reconcile")
async def reconcile(
    alert: bool = False,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Verify supplier_balances against a replay of purchases - payments.

    If `alert=true` and any drift is found, fires a Discord webhook (reusing
    core/notify.py from the gold-rate poller).

    Coin/ounce stock reconciliation is NOT included here — sweeping the JSONB
    ledger payloads is expensive and the on_hand_qty column is the source of
    truth (it's locked FOR UPDATE on every change). Flag if you want a deeper
    sweep added.
    """
    expected = await _compute_expected_balances(db)

    # Stored balances (only non-zero are interesting, but include all for completeness)
    stored_rows = (await db.execute(select(SupplierBalance))).scalars().all()
    stored: dict[tuple[str, str, str], Decimal] = {
        (r.supplier_id, r.unit.value, r.karat): Decimal(str(r.balance))
        for r in stored_rows
    }

    all_keys = set(expected.keys()) | set(stored.keys())
    drifts = []
    for key in all_keys:
        exp = expected.get(key, Decimal("0"))
        got = stored.get(key, Decimal("0"))
        if exp != got:
            drifts.append({
                "supplier_id": key[0],
                "unit": key[1],
                "karat": key[2] if key[2] else None,
                "stored": str(got),
                "computed": str(exp),
                "drift": str(got - exp),
            })

    # Hydrate supplier names for drift entries
    if drifts:
        supplier_ids = {d["supplier_id"] for d in drifts}
        names = {
            s.id: s.name
            for s in (
                await db.execute(select(Supplier).where(Supplier.id.in_(supplier_ids)))
            ).scalars().all()
        }
        for d in drifts:
            d["supplier_name"] = names.get(d["supplier_id"], "?")

    alerted = False
    if drifts and alert:
        msg = (
            f"⚠️ Supplier-balance drift detected ({len(drifts)} row(s)).\n"
            + "\n".join(
                f"  • {d['supplier_name']} ({d['unit']}"
                + (f" K{d['karat']}" if d['karat'] else "")
                + f"): stored={d['stored']}, expected={d['computed']}, drift={d['drift']}"
                for d in drifts[:10]
            )
        )
        try:
            await send_discord_alert(msg)
            alerted = True
        except Exception:
            alerted = False

    return {
        "supplier_balance_drifts": drifts,
        "drift_count": len(drifts),
        "discord_alerted": alerted,
    }


# ── Coin / ounce stock reconciliation (audit phase B1) ───────────────────────


async def _expected_unit_qty(
    db: AsyncSession,
    *,
    kind: Literal["COIN", "OUNCE"],
    unit_type_id: str,
) -> int:
    """Replay every event that mutated this unit's `on_hand_qty` and return
    the sum the history implies.

    AUDIT: see plan §B1. Sources:
      +SupplierPurchaseItem.quantity   (item_kind matches, ref matches)
      +WalkinBuyback.quantity           (kind matches, ref matches)
      +ManualAdjustment.delta           (target_type matches, target_id matches)
      −OrderItem.quantity               (item_kind matches, ref matches,
                                          Order.status IN (COMPLETED, REFUNDED))

    VOIDED orders are skipped entirely — the sale subtracted and the void
    restored, so they net to zero. REFUNDED orders subtract (items left and
    didn't come back; refund is money-side only). No melt term — coins/ounces
    cannot be melted via current code (confirmed in [app/api/melts.py]).
    """
    if kind == "COIN":
        item_kind = OrderItemKind.COIN
        buyback_kind = BuybackKind.COIN
        supplier_kind = SupplierItemKind.COIN
        adjustment_target = AdjustmentTarget.COIN_STOCK
        order_item_ref = OrderItem.coin_type_id
        buyback_ref = WalkinBuyback.coin_type_id
        purchase_item_ref = SupplierPurchaseItem.coin_type_id
    else:
        item_kind = OrderItemKind.OUNCE
        buyback_kind = BuybackKind.OUNCE
        supplier_kind = SupplierItemKind.OUNCE
        adjustment_target = AdjustmentTarget.OUNCE_STOCK
        order_item_ref = OrderItem.ounce_type_id
        buyback_ref = WalkinBuyback.ounce_type_id
        purchase_item_ref = SupplierPurchaseItem.ounce_type_id

    # +: supplier purchases
    plus_purchases = (
        await db.execute(
            select(func.coalesce(func.sum(SupplierPurchaseItem.quantity), 0)).where(
                SupplierPurchaseItem.item_kind == supplier_kind,
                purchase_item_ref == unit_type_id,
            )
        )
    ).scalar_one()

    # +: walkin buybacks
    plus_buybacks = (
        await db.execute(
            select(func.coalesce(func.sum(WalkinBuyback.quantity), 0)).where(
                WalkinBuyback.kind == buyback_kind,
                buyback_ref == unit_type_id,
            )
        )
    ).scalar_one()

    # +/-: manual adjustments (delta is signed, signed sum is correct)
    net_adjustments = (
        await db.execute(
            select(func.coalesce(func.sum(ManualAdjustment.delta), 0)).where(
                ManualAdjustment.target_type == adjustment_target,
                ManualAdjustment.target_id == unit_type_id,
            )
        )
    ).scalar_one()

    # -: order items from COMPLETED + REFUNDED orders (VOIDED excluded)
    minus_sales = (
        await db.execute(
            select(func.coalesce(func.sum(OrderItem.quantity), 0))
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                OrderItem.item_kind == item_kind,
                order_item_ref == unit_type_id,
                Order.status.in_((OrderStatus.COMPLETED, OrderStatus.REFUNDED)),
            )
        )
    ).scalar_one()

    expected = (
        int(plus_purchases)
        + int(plus_buybacks)
        + int(net_adjustments)
        - int(minus_sales)
    )
    return expected


@router.get("/reconcile-units")
async def reconcile_units(
    alert: bool = False,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Verify CoinType.on_hand_qty + OunceType.on_hand_qty against a replay
    of the event history.

    AUDIT: read-only. Reports drift per type but never mutates. Resolving
    drift is a separate operation — either find the missing event in the
    code, or run a physical stock-take (audit phase B2) and post a
    `MANUAL_ADJUSTMENT` for the variance.

    Perf: O(types × queries-per-type). For the current dev DB scale this
    is sub-second. At ten thousand types this would need batching into a
    single grouped query — out of scope today.
    """
    drifts: list[dict] = []

    for ct in (await db.execute(select(CoinType))).scalars().all():
        expected = await _expected_unit_qty(db, kind="COIN", unit_type_id=ct.id)
        if expected != ct.on_hand_qty:
            drifts.append({
                "kind": "COIN",
                "id": ct.id,
                "code": ct.code,
                "name_en": ct.name_en,
                "stored": ct.on_hand_qty,
                "computed": expected,
                "drift": ct.on_hand_qty - expected,
            })

    for ot in (await db.execute(select(OunceType))).scalars().all():
        expected = await _expected_unit_qty(db, kind="OUNCE", unit_type_id=ot.id)
        if expected != ot.on_hand_qty:
            drifts.append({
                "kind": "OUNCE",
                "id": ot.id,
                "code": ot.code,
                "name_en": ot.name_en,
                "stored": ot.on_hand_qty,
                "computed": expected,
                "drift": ot.on_hand_qty - expected,
            })

    alerted = False
    if drifts and alert:
        msg = (
            f"⚠️ Coin/ounce stock drift detected ({len(drifts)} row(s)).\n"
            + "\n".join(
                f"  • {d['kind']} {d['code']} ({d['name_en']}): "
                f"stored={d['stored']}, expected={d['computed']}, drift={d['drift']:+d}"
                for d in drifts[:10]
            )
        )
        try:
            await send_discord_alert(msg)
            alerted = True
        except Exception:
            alerted = False

    return {
        "unit_drifts": drifts,
        "drift_count": len(drifts),
        "discord_alerted": alerted,
    }
