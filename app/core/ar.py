"""Accounts Receivable (Module 3): customer subledger, credit invoices, receipts
(FIFO allocation), aging, statements, AR-control tie-out.

The subledger (invoices/receipts) is maintained on every credit sale/receipt.
The GL is posted only when the accounting flag is ON (gl_entry_id nullable).
Customer balance is always DERIVED from the subledger, never stored."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import gl
from app.core.gl_postings import auto_post_enabled
from app.models import (
    ARInvoice, ARInvoiceLine, ARInvoiceStatus, ARReceipt, ARReceiptAllocation,
    Customer, GLAccount, GLEntrySequence, GLJournalLine, Settings,
)

ZERO = Decimal("0")
_Q_MONEY = Decimal("0.01")

SOURCE_AR_INVOICE = "AR_INVOICE"
SOURCE_AR_RECEIPT = "AR_RECEIPT"


async def _next_doc_no(db: AsyncSession, prefix: str, entry_date: date) -> str:
    day_key = f"{prefix}{entry_date.strftime('%Y%m%d')}"
    row = (await db.execute(
        select(GLEntrySequence).where(GLEntrySequence.day_key == day_key).with_for_update()
    )).scalar_one_or_none()
    if row is None:
        row = GLEntrySequence(day_key=day_key, last_seq=0)
        db.add(row)
        await db.flush()
    row.last_seq += 1
    await db.flush()
    return f"{prefix}-{entry_date.strftime('%Y%m%d')}-{row.last_seq:03d}"


async def customer_open_balance(db: AsyncSession, customer_id: str) -> Decimal:
    invoices = (await db.execute(
        select(ARInvoice).where(
            ARInvoice.customer_id == customer_id,
            ARInvoice.status.in_((ARInvoiceStatus.OPEN, ARInvoiceStatus.PARTIAL)),
        )
    )).scalars().all()
    owed = sum(((inv.total - inv.amount_paid) for inv in invoices), ZERO)
    receipts = (await db.execute(
        select(ARReceipt).where(ARReceipt.customer_id == customer_id)
    )).scalars().all()
    unapplied = sum(((r.unapplied_amount) for r in receipts), ZERO)
    return (owed - unapplied).quantize(_Q_MONEY)


async def check_credit_limit(db: AsyncSession, customer: Customer, additional: Decimal) -> None:
    if customer.credit_limit is None:
        return
    bal = await customer_open_balance(db, customer.id)
    if (bal + additional) > customer.credit_limit:
        raise HTTPException(
            status_code=422,
            detail=(f"Credit limit exceeded for {customer.name}: balance {bal} + {additional} "
                    f"> limit {customer.credit_limit}"),
        )


async def _resolve(db: AsyncSession, system_key: str) -> str:
    a = (await db.execute(select(GLAccount).where(GLAccount.system_key == system_key))).scalar_one_or_none()
    if a is None:
        raise HTTPException(422, f"GL account {system_key} not seeded")
    return a.id


async def create_invoice_from_order(db: AsyncSession, *, order, customer_id: str,
                                    gl_entry, actor_user_id: str) -> ARInvoice:
    when = order.created_at.date() if order.created_at else date.today()
    inv = ARInvoice(
        invoice_no=await _next_doc_no(db, "AR", when),
        customer_id=customer_id, order_id=order.id, invoice_date=when, currency="USD",
        subtotal=order.subtotal.quantize(_Q_MONEY), vat_amount=order.vat_amount.quantize(_Q_MONEY),
        total=order.total_usd.quantize(_Q_MONEY), status=ARInvoiceStatus.OPEN,
        gl_entry_id=(gl_entry.id if gl_entry else None), memo=f"Credit sale {order.order_number}",
    )
    db.add(inv)
    await db.flush()
    for it in order.items:
        up = (it.final_price / it.quantity if it.quantity else it.final_price).quantize(_Q_MONEY)
        db.add(ARInvoiceLine(invoice_id=inv.id, description=it.product_name, quantity=it.quantity,
                             unit_price=up, line_total=it.final_price.quantize(_Q_MONEY)))
    await db.flush()
    return inv


async def verify_ar_control(db: AsyncSession) -> dict:
    ar_acct = (await db.execute(select(GLAccount).where(GLAccount.system_key == "AR"))).scalar_one()
    gl_lines = (await db.execute(select(GLJournalLine).where(GLJournalLine.account_id == ar_acct.id))).scalars().all()
    gl_ar = sum((l.base_debit - l.base_credit for l in gl_lines), ZERO).quantize(_Q_MONEY)
    customers = (await db.execute(select(Customer))).scalars().all()
    sub = ZERO
    for c in customers:
        sub += await customer_open_balance(db, c.id)
    sub = sub.quantize(_Q_MONEY)
    return {"gl_ar_balance": gl_ar, "subledger_balance": sub, "matches": gl_ar == sub}


async def post_standalone_invoice(db: AsyncSession, *, customer_id: str, invoice_date: date,
                                  due_date, lines: list[dict], memo: str, vat_percent: Decimal,
                                  settings: Settings, actor_user_id: str) -> ARInvoice:
    customer = (await db.execute(select(Customer).where(Customer.id == customer_id))).scalar_one_or_none()
    if customer is None:
        raise HTTPException(404, "Customer not found")
    subtotal = sum((Decimal(str(l["unit_price"])) * int(l.get("quantity", 1)) for l in lines), ZERO).quantize(_Q_MONEY)
    vat = (subtotal * Decimal(str(vat_percent)) / Decimal(100)).quantize(_Q_MONEY)
    total = (subtotal + vat).quantize(_Q_MONEY)
    await check_credit_limit(db, customer, total)

    entry = None
    if auto_post_enabled(settings):
        ar_id = await _resolve(db, "AR")
        rev_id = await _resolve(db, "SALES_REVENUE")
        vat_id = await _resolve(db, "VAT_PAYABLE")
        gl_lines = [gl.GLLine(account_id=ar_id, denomination="MONEY", base_debit=total, money_debit=total, memo="AR invoice"),
                    gl.GLLine(account_id=rev_id, denomination="MONEY", base_credit=subtotal, money_credit=subtotal, memo="revenue")]
        if vat > 0:
            gl_lines.append(gl.GLLine(account_id=vat_id, denomination="MONEY", base_credit=vat, money_credit=vat, memo="VAT"))
        entry = await gl.post_entry(db, entry_date=invoice_date, memo=f"AR invoice {memo}",
                                    source_type=SOURCE_AR_INVOICE, source_id=None, lines=gl_lines, actor_user_id=actor_user_id)

    inv = ARInvoice(invoice_no=await _next_doc_no(db, "AR", invoice_date), customer_id=customer_id,
                    invoice_date=invoice_date, due_date=due_date, currency=customer.currency,
                    subtotal=subtotal, vat_amount=vat, total=total, status=ARInvoiceStatus.OPEN,
                    gl_entry_id=(entry.id if entry else None), memo=memo)
    db.add(inv)
    await db.flush()
    for l in lines:
        qty = int(l.get("quantity", 1))
        up = Decimal(str(l["unit_price"])).quantize(_Q_MONEY)
        db.add(ARInvoiceLine(invoice_id=inv.id, description=l.get("description", ""),
                             quantity=qty, unit_price=up, line_total=(up * qty).quantize(_Q_MONEY)))
    await db.flush()
    return inv


def _apply_to_invoice(inv: ARInvoice, amount: Decimal) -> Decimal:
    """Apply up to `amount` to an invoice; return the amount actually applied."""
    owed = (inv.total - inv.amount_paid)
    applied = min(amount, owed)
    inv.amount_paid = (inv.amount_paid + applied).quantize(_Q_MONEY)
    inv.status = ARInvoiceStatus.PAID if inv.amount_paid >= inv.total else ARInvoiceStatus.PARTIAL
    return applied


async def post_receipt(db: AsyncSession, *, customer_id: str, receipt_date: date, amount: Decimal,
                       payment_system_key: str, memo: str, settings: Settings, actor_user_id: str,
                       allocations: list[dict] | None = None) -> ARReceipt:
    amount = Decimal(str(amount)).quantize(_Q_MONEY)
    if amount <= 0:
        raise HTTPException(422, "amount must be positive")

    entry = None
    if auto_post_enabled(settings):
        pay_id = await _resolve(db, payment_system_key)
        ar_id = await _resolve(db, "AR")
        entry = await gl.post_entry(db, entry_date=receipt_date, memo=f"AR receipt {memo}",
            source_type=SOURCE_AR_RECEIPT, source_id=None,
            lines=[gl.GLLine(account_id=pay_id, denomination="MONEY", base_debit=amount, money_debit=amount, memo="receipt"),
                   gl.GLLine(account_id=ar_id, denomination="MONEY", base_credit=amount, money_credit=amount, memo="apply AR")],
            actor_user_id=actor_user_id)

    receipt = ARReceipt(receipt_no=await _next_doc_no(db, "RC", receipt_date), customer_id=customer_id,
                        receipt_date=receipt_date, amount=amount, payment_system_key=payment_system_key,
                        gl_entry_id=(entry.id if entry else None), memo=memo)
    db.add(receipt)
    await db.flush()

    remaining = amount
    if allocations:
        for al in allocations:
            inv = (await db.execute(select(ARInvoice).where(ARInvoice.id == al["invoice_id"]))).scalar_one()
            applied = _apply_to_invoice(inv, Decimal(str(al["amount"])).quantize(_Q_MONEY))
            db.add(ARReceiptAllocation(receipt_id=receipt.id, invoice_id=inv.id, amount=applied))
            remaining -= applied
    else:
        open_invoices = (await db.execute(
            select(ARInvoice).where(ARInvoice.customer_id == customer_id,
                                    ARInvoice.status.in_((ARInvoiceStatus.OPEN, ARInvoiceStatus.PARTIAL)))
            .order_by(ARInvoice.invoice_date, ARInvoice.invoice_no)
        )).scalars().all()
        for inv in open_invoices:
            if remaining <= 0:
                break
            applied = _apply_to_invoice(inv, remaining)
            if applied > 0:
                db.add(ARReceiptAllocation(receipt_id=receipt.id, invoice_id=inv.id, amount=applied))
                remaining -= applied

    receipt.unapplied_amount = remaining.quantize(_Q_MONEY)
    await db.flush()
    return receipt


def _bucket(days: int) -> str:
    if days <= 30:
        return "0_30"
    if days <= 60:
        return "31_60"
    if days <= 90:
        return "61_90"
    return "90_plus"


async def compute_aging(db: AsyncSession, *, as_of: date, customer_id: str | None = None) -> dict:
    q = select(ARInvoice).where(ARInvoice.status.in_((ARInvoiceStatus.OPEN, ARInvoiceStatus.PARTIAL)))
    if customer_id:
        q = q.where(ARInvoice.customer_id == customer_id)
    invoices = (await db.execute(q)).scalars().all()
    empty = {"0_30": ZERO, "31_60": ZERO, "61_90": ZERO, "90_plus": ZERO}
    totals = dict(empty)
    by_customer: dict[str, dict] = {}
    for inv in invoices:
        owed = (inv.total - inv.amount_paid)
        if owed <= 0:
            continue
        b = _bucket((as_of - inv.invoice_date).days)
        totals[b] += owed
        cust = by_customer.setdefault(inv.customer_id, dict(empty))
        cust[b] += owed

    def _q(d):
        return {k: v.quantize(_Q_MONEY) for k, v in d.items()}
    return {"as_of": as_of, "totals": _q(totals),
            "by_customer": {cid: _q(v) for cid, v in by_customer.items()},
            "grand_total": sum(totals.values(), ZERO).quantize(_Q_MONEY)}


async def customer_statement(db: AsyncSession, customer_id: str, *, from_date: date, until: date) -> dict:
    invoices = (await db.execute(
        select(ARInvoice).where(ARInvoice.customer_id == customer_id,
                                ARInvoice.invoice_date >= from_date, ARInvoice.invoice_date <= until)
        .order_by(ARInvoice.invoice_date)
    )).scalars().all()
    receipts = (await db.execute(
        select(ARReceipt).where(ARReceipt.customer_id == customer_id,
                                ARReceipt.receipt_date >= from_date, ARReceipt.receipt_date <= until)
        .order_by(ARReceipt.receipt_date)
    )).scalars().all()
    events = (
        [{"date": i.invoice_date, "kind": "invoice", "ref": i.invoice_no, "debit": i.total, "credit": ZERO} for i in invoices]
        + [{"date": r.receipt_date, "kind": "receipt", "ref": r.receipt_no, "debit": ZERO, "credit": r.amount} for r in receipts]
    )
    events.sort(key=lambda e: (e["date"], e["kind"]))
    running = ZERO
    for e in events:
        running += e["debit"] - e["credit"]
        e["balance"] = running.quantize(_Q_MONEY)
        e["debit"] = e["debit"].quantize(_Q_MONEY)
        e["credit"] = e["credit"].quantize(_Q_MONEY)
    return {"customer_id": customer_id, "from": from_date, "until": until,
            "events": events, "closing_balance": running.quantize(_Q_MONEY)}
