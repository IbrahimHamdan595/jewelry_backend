"""Phase 7 — inventory alerts + supplier-balance reconciliation."""

from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.notify import send_discord_alert
from app.core.permissions import require_admin
from app.deps import get_db
from app.models import (
    CoinType,
    DebtUnit,
    OunceType,
    Supplier,
    SupplierBalance,
    SupplierPayment,
    SupplierPurchase,
    User,
)

router = APIRouter(prefix="/inventory", tags=["inventory"])


# ── Low-stock alerts ──────────────────────────────────────────────────────────


@router.get("/alerts")
async def inventory_alerts(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Coin and ounce types whose on_hand_qty is at-or-below their min_stock_qty.

    Pure-gold lots are intentionally excluded (working pool, not SKUs).
    Atomic products are excluded for the same reason (no quantity concept).
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
