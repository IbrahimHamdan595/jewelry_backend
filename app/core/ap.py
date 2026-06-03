"""Accounts Payable (Module 4): three-way tie-out, FIFO-reconstructed aging, and
supplier statements over the existing supplier tables. Pure reporting — no GL
writes (M0 opening + M1 auto-posting already post AP/Metal AP)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DebtUnit, GLAccount, GLJournalLine, Supplier, SupplierBalance, SupplierPayment,
    SupplierPurchase,
)

ZERO = Decimal("0")
_Q_MONEY = Decimal("0.01")
_Q_GRAMS = Decimal("0.001")


async def _gl_account_lines(db: AsyncSession, system_key: str):
    acct = (await db.execute(select(GLAccount).where(GLAccount.system_key == system_key))).scalar_one()
    return (await db.execute(select(GLJournalLine).where(GLJournalLine.account_id == acct.id))).scalars().all()


async def verify_ap_control(db: AsyncSession) -> dict:
    # Cash AP: GL AP is CR-normal; owed = credits − debits.
    ap_lines = await _gl_account_lines(db, "AP")
    gl_ap = sum((l.base_credit - l.base_debit for l in ap_lines), ZERO).quantize(_Q_MONEY)
    sub_cash_rows = (await db.execute(
        select(SupplierBalance).where(SupplierBalance.unit == DebtUnit.CASH)
    )).scalars().all()
    sub_ap = sum((r.balance for r in sub_cash_rows), ZERO)
    ap_block = {"gl": gl_ap, "subledger": sub_ap.quantize(_Q_MONEY), "matches": gl_ap == sub_ap.quantize(_Q_MONEY)}

    # Metal AP per karat: GL metal credits − debits per karat vs SupplierBalance GOLD.
    metal_lines = await _gl_account_lines(db, "METAL_AP")
    gl_metal: dict[str, Decimal] = {}
    for l in metal_lines:
        if l.metal_credit_grams or l.metal_debit_grams:
            k = l.karat or "?"
            gl_metal[k] = gl_metal.get(k, ZERO) + (l.metal_credit_grams - l.metal_debit_grams)
    sub_gold_rows = (await db.execute(
        select(SupplierBalance).where(SupplierBalance.unit == DebtUnit.GOLD)
    )).scalars().all()
    sub_metal: dict[str, Decimal] = {}
    for r in sub_gold_rows:
        sub_metal[r.karat] = sub_metal.get(r.karat, ZERO) + r.balance
    karats = set(gl_metal) | set(sub_metal)
    by_karat = {}
    all_match = True
    for k in sorted(karats):
        g = gl_metal.get(k, ZERO).quantize(_Q_GRAMS)
        s = sub_metal.get(k, ZERO).quantize(_Q_GRAMS)
        m = g == s
        all_match = all_match and m
        by_karat[k] = {"gl": g, "subledger": s, "matches": m}

    return {"ap": ap_block, "metal_ap": {"by_karat": by_karat, "matches": all_match}}


def _bucket(days: int) -> str:
    if days <= 30:
        return "0_30"
    if days <= 60:
        return "31_60"
    if days <= 90:
        return "61_90"
    return "90_plus"


async def compute_ap_aging(db: AsyncSession, *, as_of: date) -> dict:
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


async def supplier_statement(db: AsyncSession, supplier_id: str, *, from_date: date, until: date) -> dict:
    purchases = (await db.execute(
        select(SupplierPurchase).where(SupplierPurchase.supplier_id == supplier_id)
        .order_by(SupplierPurchase.occurred_at)
    )).scalars().all()
    payments = (await db.execute(
        select(SupplierPayment).where(SupplierPayment.supplier_id == supplier_id,
                                      SupplierPayment.unit == DebtUnit.CASH)
        .order_by(SupplierPayment.paid_at)
    )).scalars().all()
    events = []
    for p in purchases:
        d = p.occurred_at.date() if p.occurred_at else from_date
        if from_date <= d <= until:
            net = (p.total_cash_due or ZERO) - (p.cash_paid_at_creation or ZERO)
            events.append({"date": d, "kind": "purchase", "debit": net.quantize(_Q_MONEY), "credit": ZERO})
    for pay in payments:
        d = pay.paid_at.date() if pay.paid_at else from_date
        if from_date <= d <= until:
            events.append({"date": d, "kind": "payment", "debit": ZERO, "credit": pay.amount.quantize(_Q_MONEY)})
    events.sort(key=lambda e: (e["date"], e["kind"]))
    running = ZERO
    for e in events:
        running += e["debit"] - e["credit"]
        e["balance"] = running.quantize(_Q_MONEY)
        e["debit"] = e["debit"].quantize(_Q_MONEY)
        e["credit"] = e["credit"].quantize(_Q_MONEY)
    gold_rows = (await db.execute(
        select(SupplierBalance).where(SupplierBalance.supplier_id == supplier_id,
                                      SupplierBalance.unit == DebtUnit.GOLD)
    )).scalars().all()
    return {"supplier_id": supplier_id, "from": from_date, "until": until, "events": events,
            "closing_cash_balance": running.quantize(_Q_MONEY),
            "gold_owed_by_karat": {r.karat: r.balance.quantize(_Q_GRAMS) for r in gold_rows if r.balance != 0}}
