"""Expenses & Purchasing (Module 5): vendor bills (paid-now or on-credit), vendor
payments (FIFO), VENDOR_AP tie-out, expense + vendor-spend reports. Money-only;
direct entry (no approval). Subledger maintained always; GL posts when the flag
is ON."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import gl, gl_fx
from app.core.ar import _leg_to_glline, _next_doc_no
from app.core.gl_postings import auto_post_enabled
from app.models import (
    AccountType, GLAccount, GLJournalEntry, GLJournalLine, Settings, TaxCode,
    VendorBill, VendorBillLine, VendorBillStatus, VendorPayment, VendorPaymentAllocation,
)

ZERO = Decimal("0")
_Q_MONEY = Decimal("0.01")
ONE = Decimal("1")

SOURCE_VENDOR_BILL = "VENDOR_BILL"
SOURCE_VENDOR_PAYMENT = "VENDOR_PAYMENT"


async def _resolve(db: AsyncSession, system_key: str) -> str:
    a = (await db.execute(select(GLAccount).where(GLAccount.system_key == system_key))).scalar_one_or_none()
    if a is None:
        raise HTTPException(422, f"GL account {system_key} not seeded")
    return a.id


async def _resolve_expense_account(db: AsyncSession, line: dict) -> str:
    if line.get("expense_account_id"):
        acct = (await db.execute(select(GLAccount).where(GLAccount.id == line["expense_account_id"]))).scalar_one_or_none()
    else:
        acct = (await db.execute(select(GLAccount).where(GLAccount.system_key == line["expense_system_key"]))).scalar_one_or_none()
    if acct is None:
        raise HTTPException(422, "Expense account not found")
    if acct.type != AccountType.EXPENSE:
        raise HTTPException(422, f"Account {acct.code} is not an EXPENSE account")
    return acct.id


async def post_vendor_bill(db: AsyncSession, *, vendor_name: str, supplier_id: str | None,
                           bill_date: date, due_date, lines: list[dict], payment_system_key: str | None,
                           memo: str, settings: Settings, actor_user_id: str,
                           tax_code_id: str | None = None,
                           currency: str = "USD", fx_rate: Decimal | None = None) -> VendorBill:
    if not lines:
        raise HTTPException(422, "A bill needs at least one line")
    currency = currency or "USD"
    rate = (Decimal(str(fx_rate)) if fx_rate is not None
            else (Decimal(str(settings.lbp_exchange_rate)) if currency != "USD" else ONE))
    resolved = []  # (expense_account_id, amount, description)
    total = ZERO
    for ln in lines:
        acct_id = await _resolve_expense_account(db, ln)
        amt = Decimal(str(ln["amount"])).quantize(_Q_MONEY)
        resolved.append((acct_id, amt, ln.get("description", "")))
        total += amt

    # Module 6 — input VAT. Lines are NET; VAT is added on top per the tax code.
    subtotal = total.quantize(_Q_MONEY)
    vat = ZERO
    if tax_code_id:
        tc = (await db.execute(select(TaxCode).where(TaxCode.id == tax_code_id))).scalar_one_or_none()
        if tc is None:
            raise HTTPException(422, "Tax code not found")
        vat = (subtotal * tc.rate / Decimal(100)).quantize(_Q_MONEY)
    total = (subtotal + vat).quantize(_Q_MONEY)
    base_total = (total / rate).quantize(_Q_MONEY)  # money is in the bill currency; base = money/rate

    entry = None
    if auto_post_enabled(settings):
        credit_id = (await _resolve(db, payment_system_key)) if payment_system_key else (await _resolve(db, "VENDOR_AP"))
        gl_lines = [gl.GLLine(account_id=aid, denomination="MONEY", base_debit=(amt / rate).quantize(_Q_MONEY),
                              money_debit=amt, currency=currency, fx_rate=rate, memo=desc or "expense")
                    for aid, amt, desc in resolved]
        if vat > 0:
            vat_id = await _resolve(db, "VAT_RECEIVABLE")
            gl_lines.append(gl.GLLine(account_id=vat_id, denomination="MONEY", base_debit=(vat / rate).quantize(_Q_MONEY),
                                      money_debit=vat, currency=currency, fx_rate=rate, memo="input VAT"))
        gl_lines.append(gl.GLLine(account_id=credit_id, denomination="MONEY",
                                  base_credit=base_total, money_credit=total, currency=currency, fx_rate=rate,
                                  memo=("paid" if payment_system_key else "vendor AP")))
        entry = await gl.post_entry(db, entry_date=bill_date, memo=f"Vendor bill {vendor_name} {memo}",
                                    source_type=SOURCE_VENDOR_BILL, source_id=None, lines=gl_lines, actor_user_id=actor_user_id)

    paid_now = payment_system_key is not None
    bill = VendorBill(bill_no=await _next_doc_no(db, "BILL", bill_date), vendor_name=vendor_name,
                      supplier_id=supplier_id, bill_date=bill_date, due_date=due_date,
                      currency=currency, fx_rate=rate,
                      subtotal=subtotal, vat_amount=vat, total=total, tax_code_id=tax_code_id,
                      amount_paid=(total if paid_now else ZERO),
                      status=(VendorBillStatus.PAID if paid_now else VendorBillStatus.OPEN),
                      payment_system_key=payment_system_key, gl_entry_id=(entry.id if entry else None), memo=memo)
    db.add(bill)
    await db.flush()
    for aid, amt, desc in resolved:
        db.add(VendorBillLine(bill_id=bill.id, description=desc, expense_account_id=aid, amount=amt))
    await db.flush()
    return bill


def _apply(bill: VendorBill, amount: Decimal) -> Decimal:
    owed = bill.total - bill.amount_paid
    applied = min(amount, owed)
    bill.amount_paid = (bill.amount_paid + applied).quantize(_Q_MONEY)
    bill.status = VendorBillStatus.PAID if bill.amount_paid >= bill.total else VendorBillStatus.PARTIAL
    return applied


async def post_vendor_payment(db: AsyncSession, *, vendor_name: str, payment_date: date, amount: Decimal,
                              payment_system_key: str, memo: str, settings: Settings, actor_user_id: str,
                              allocations: list[dict] | None = None,
                              currency: str = "USD", fx_rate: Decimal | None = None) -> VendorPayment:
    amount = Decimal(str(amount)).quantize(_Q_MONEY)
    if amount <= 0:
        raise HTTPException(422, "amount must be positive")
    currency = currency or "USD"
    cash_acct = (await db.execute(
        select(GLAccount).where(GLAccount.system_key == payment_system_key))).scalar_one_or_none()
    if cash_acct is None:
        raise HTTPException(422, f"GL account {payment_system_key} not seeded")
    tender_currency = cash_acct.currency or "USD"
    rate = (Decimal(str(fx_rate)) if fx_rate is not None
            else (Decimal(str(settings.lbp_exchange_rate)) if tender_currency != "USD" else ONE))

    payment = VendorPayment(payment_no=await _next_doc_no(db, "VP", payment_date), vendor_name=vendor_name,
                            payment_date=payment_date, currency=currency, fx_rate=rate, amount=amount,
                            payment_system_key=payment_system_key, gl_entry_id=None, memo=memo)
    db.add(payment)
    await db.flush()

    alloc_specs: list[gl_fx.Allocation] = []
    remaining = amount
    if allocations:
        for al in allocations:
            bill = (await db.execute(select(VendorBill).where(VendorBill.id == al["bill_id"]))).scalar_one()
            applied = _apply(bill, Decimal(str(al["amount"])).quantize(_Q_MONEY))
            if applied > 0:
                db.add(VendorPaymentAllocation(payment_id=payment.id, bill_id=bill.id, amount=applied))
                alloc_specs.append(gl_fx.Allocation(bill.currency or "USD", bill.fx_rate or ONE, applied))
                remaining -= applied
    else:
        open_bills = (await db.execute(
            select(VendorBill).where(VendorBill.vendor_name == vendor_name,
                                     VendorBill.currency == currency,
                                     VendorBill.status.in_((VendorBillStatus.OPEN, VendorBillStatus.PARTIAL)))
            .order_by(VendorBill.bill_date, VendorBill.bill_no)
        )).scalars().all()
        for bill in open_bills:
            if remaining <= 0:
                break
            applied = _apply(bill, remaining)
            if applied > 0:
                db.add(VendorPaymentAllocation(payment_id=payment.id, bill_id=bill.id, amount=applied))
                alloc_specs.append(gl_fx.Allocation(bill.currency or "USD", bill.fx_rate or ONE, applied))
                remaining -= applied

    payment.unapplied_amount = remaining.quantize(_Q_MONEY)

    if auto_post_enabled(settings):
        specs = list(alloc_specs)
        if remaining > ZERO:   # overpayment debits AP at the recorded rate (no FX on it)
            specs.append(gl_fx.Allocation(currency, ONE if currency == "USD" else rate, remaining.quantize(_Q_MONEY)))
        if specs:
            try:
                legs = gl_fx.settlement_legs(
                    kind="payment", recorded_currency=currency, allocations=specs,
                    control_system_key="VENDOR_AP", cash_system_key=payment_system_key,
                    tender_currency=tender_currency, tender_fx_rate=rate)
            except ValueError as e:
                raise HTTPException(422, str(e))
            gl_lines = [await _leg_to_glline(db, l) for l in legs]
            entry = await gl.post_entry(db, entry_date=payment_date, memo=f"Vendor payment {vendor_name} {memo}",
                                        source_type=SOURCE_VENDOR_PAYMENT, source_id=None,
                                        lines=gl_lines, actor_user_id=actor_user_id)
            payment.gl_entry_id = entry.id

    await db.flush()
    return payment


async def verify_vendor_ap(db: AsyncSession) -> dict:
    ap_acct = (await db.execute(select(GLAccount).where(GLAccount.system_key == "VENDOR_AP"))).scalar_one()
    lines = (await db.execute(select(GLJournalLine).where(GLJournalLine.account_id == ap_acct.id))).scalars().all()
    gl_ap = sum((l.base_credit - l.base_debit for l in lines), ZERO).quantize(_Q_MONEY)
    bills = (await db.execute(
        select(VendorBill).where(VendorBill.status.in_((VendorBillStatus.OPEN, VendorBillStatus.PARTIAL)))
    )).scalars().all()
    # Subledger in base (USD) — each doc at its captured rate — to tie like-for-like.
    owed = sum((((b.total - b.amount_paid) / (b.fx_rate or ONE)) for b in bills), ZERO)
    payments = (await db.execute(select(VendorPayment))).scalars().all()
    unapplied = sum(((p.unapplied_amount / (p.fx_rate or ONE)) for p in payments), ZERO)
    sub = (owed - unapplied).quantize(_Q_MONEY)
    return {"gl": gl_ap, "subledger": sub, "matches": gl_ap == sub}


async def expense_by_category(db: AsyncSession, *, from_date: date, until: date) -> dict:
    exp_accts = (await db.execute(select(GLAccount).where(GLAccount.type == AccountType.EXPENSE))).scalars().all()
    by_id = {a.id: a for a in exp_accts}
    rows = (await db.execute(
        select(GLJournalLine, GLJournalEntry)
        .join(GLJournalEntry, GLJournalLine.entry_id == GLJournalEntry.id)
        .where(GLJournalEntry.entry_date >= from_date, GLJournalEntry.entry_date <= until)
    )).all()
    totals: dict[str, Decimal] = {}
    for line, entry in rows:
        if line.account_id in by_id:
            totals[line.account_id] = totals.get(line.account_id, ZERO) + (line.base_debit - line.base_credit)
    accounts = []
    grand = ZERO
    for aid, amt in totals.items():
        if amt == 0:
            continue
        a = by_id[aid]
        accounts.append({"code": a.code, "name": a.name, "system_key": a.system_key, "amount": amt.quantize(_Q_MONEY)})
        grand += amt
    accounts.sort(key=lambda x: x["code"])
    return {"from": from_date, "until": until, "accounts": accounts, "total": grand.quantize(_Q_MONEY)}


async def vendor_spend(db: AsyncSession, *, from_date: date, until: date) -> dict:
    bills = (await db.execute(
        select(VendorBill).where(VendorBill.bill_date >= from_date, VendorBill.bill_date <= until)
    )).scalars().all()
    by_vendor: dict[str, Decimal] = {}
    for b in bills:
        by_vendor[b.vendor_name] = by_vendor.get(b.vendor_name, ZERO) + b.total
    vendors = [{"vendor_name": k, "total": v.quantize(_Q_MONEY)} for k, v in sorted(by_vendor.items())]
    return {"from": from_date, "until": until, "vendors": vendors,
            "total": sum(by_vendor.values(), ZERO).quantize(_Q_MONEY)}
