"""Per-supplier AP aging — the test oracle for the grouped-query version.

`compute_ap_aging_per_supplier` is `app.core.ap.compute_ap_aging` exactly as it
stood before NEX-53: one query for the suppliers, then three more for every
supplier (purchases, cash payments, gold balances). tests/test_ap_reports.py
asserts the grouped version returns the same result. The body is unchanged
apart from the function name — do not "tidy" it.
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DebtUnit, Supplier, SupplierBalance, SupplierPayment, SupplierPurchase

ZERO = Decimal("0")
_Q_MONEY = Decimal("0.01")
_Q_GRAMS = Decimal("0.001")


def _bucket(days: int) -> str:
    if days <= 30:
        return "0_30"
    if days <= 60:
        return "31_60"
    if days <= 90:
        return "61_90"
    return "90_plus"


async def compute_ap_aging_per_supplier(db: AsyncSession, *, as_of: date) -> dict:
    suppliers = (await db.execute(select(Supplier))).scalars().all()
    cash_buckets = {"0_30": ZERO, "31_60": ZERO, "61_90": ZERO, "90_plus": ZERO}
    metal_owed: dict[str, Decimal] = {}
    by_supplier: dict[str, dict] = {}

    for sup in suppliers:
        purchases = (await db.execute(
            select(SupplierPurchase).where(SupplierPurchase.supplier_id == sup.id)
            .order_by(SupplierPurchase.occurred_at)
        )).scalars().all()
        outstanding = [
            {"date": (p.occurred_at.date() if p.occurred_at else as_of),
             "amt": (p.total_cash_due or ZERO) - (p.cash_paid_at_creation or ZERO)}
            for p in purchases if (p.total_cash_due or ZERO) - (p.cash_paid_at_creation or ZERO) > 0
        ]
        paid = sum((p.amount for p in (await db.execute(
            select(SupplierPayment).where(SupplierPayment.supplier_id == sup.id,
                                          SupplierPayment.unit == DebtUnit.CASH)
        )).scalars().all()), ZERO)
        for o in outstanding:
            if paid <= 0:
                break
            applied = min(paid, o["amt"])
            o["amt"] -= applied
            paid -= applied
        sup_buckets = {"0_30": ZERO, "31_60": ZERO, "61_90": ZERO, "90_plus": ZERO}
        for o in outstanding:
            if o["amt"] <= 0:
                continue
            b = _bucket((as_of - o["date"]).days)
            sup_buckets[b] += o["amt"]
            cash_buckets[b] += o["amt"]
        gold_rows = (await db.execute(
            select(SupplierBalance).where(SupplierBalance.supplier_id == sup.id,
                                          SupplierBalance.unit == DebtUnit.GOLD)
        )).scalars().all()
        sup_metal = {}
        for r in gold_rows:
            if r.balance != 0:
                sup_metal[r.karat] = (sup_metal.get(r.karat, ZERO) + r.balance).quantize(_Q_GRAMS)
                metal_owed[r.karat] = (metal_owed.get(r.karat, ZERO) + r.balance).quantize(_Q_GRAMS)
        by_supplier[sup.id] = {
            "name": sup.name,
            "cash_buckets": {k: v.quantize(_Q_MONEY) for k, v in sup_buckets.items()},
            "metal_by_karat": sup_metal,
        }

    cash_buckets = {k: v.quantize(_Q_MONEY) for k, v in cash_buckets.items()}
    return {"as_of": as_of, "cash_buckets": cash_buckets,
            "cash_total": sum(cash_buckets.values(), ZERO).quantize(_Q_MONEY),
            "metal_owed_by_karat": {k: v.quantize(_Q_GRAMS) for k, v in metal_owed.items()},
            "by_supplier": by_supplier}
