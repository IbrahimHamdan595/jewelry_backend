from datetime import date
from decimal import Decimal
from html import escape

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ar, pdf
from app.core.permissions import require_accounting
from app.deps import get_db
from app.models import ARInvoice, ARInvoiceLine, Customer, Settings, User
from app.schemas.ar import CustomerCreate, CustomerOut, ReceiptCreate, StandaloneInvoiceCreate

router = APIRouter(prefix="/accounting/ar", tags=["accounting-ar"])


# ── PDF builders (bilingual, RTL-aware via app.core.pdf) ──────────────────────

_STMT_L = {
    "en": {"title": "Account Statement", "period": "Period", "customer": "Customer",
           "date": "Date", "type": "Type", "ref": "Reference", "debit": "Debit (USD)",
           "credit": "Credit (USD)", "balance": "Balance (USD)", "closing": "Closing balance",
           "invoice": "Invoice", "receipt": "Receipt", "empty": "No activity in this period."},
    "ar": {"title": "كشف حساب", "period": "الفترة", "customer": "العميل",
           "date": "التاريخ", "type": "النوع", "ref": "المرجع", "debit": "مدين (دولار)",
           "credit": "دائن (دولار)", "balance": "الرصيد (دولار)", "closing": "الرصيد الختامي",
           "invoice": "فاتورة", "receipt": "سند قبض", "empty": "لا حركة في هذه الفترة."},
}

_INV_L = {
    "en": {"title": "Invoice", "customer": "Customer", "date": "Date", "no": "Invoice No.",
           "desc": "Description", "qty": "Qty", "price": "Unit price", "amount": "Amount",
           "subtotal": "Subtotal", "vat": "VAT", "total": "Total", "paid": "Paid", "due": "Balance due"},
    "ar": {"title": "فاتورة", "customer": "العميل", "date": "التاريخ", "no": "رقم الفاتورة",
           "desc": "الوصف", "qty": "الكمية", "price": "سعر الوحدة", "amount": "المبلغ",
           "subtotal": "المجموع الفرعي", "vat": "ضريبة القيمة المضافة", "total": "الإجمالي",
           "paid": "المدفوع", "due": "الرصيد المستحق"},
}


def _statement_html(data: dict, customer_name: str, lang: str) -> str:
    L = _STMT_L.get(lang, _STMT_L["en"])
    kind = {"invoice": L["invoice"], "receipt": L["receipt"]}
    headers = [(L["date"], False), (L["type"], False), (L["ref"], False),
               (L["debit"], True), (L["credit"], True), (L["balance"], True)]
    rows = [[str(e["date"]), kind.get(e["kind"], e["kind"]), e["ref"],
             e["debit"], e["credit"], e["balance"]] for e in data["events"]]
    tbl = pdf.table(headers, rows) if rows else f'<p class="muted">{escape(L["empty"])}</p>'
    meta = (f'<div class="meta"><div><strong>{escape(L["customer"])}:</strong> {escape(customer_name)}</div>'
            f'<div class="muted">{escape(L["period"])}: {data["from"]} → {data["until"]}</div></div>')
    summary = (f'<div class="summary"><div class="row grand"><span>{escape(L["closing"])}</span>'
               f'<span class="amt">{data["closing_balance"]}</span></div></div>')
    return pdf.document(title=L["title"], lang=lang, body=meta + tbl + summary)


def _invoice_html(inv: ARInvoice, lines: list[ARInvoiceLine], customer_name: str, lang: str) -> str:
    L = _INV_L.get(lang, _INV_L["en"])
    headers = [(L["desc"], False), (L["qty"], True), (L["price"], True), (L["amount"], True)]
    rows = [[ln.description, ln.quantity, ln.unit_price, ln.line_total] for ln in lines]
    tbl = pdf.table(headers, rows)
    meta = (f'<div class="meta"><div><strong>{escape(L["no"])}:</strong> {escape(inv.invoice_no)}</div>'
            f'<div><strong>{escape(L["customer"])}:</strong> {escape(customer_name)}</div>'
            f'<div class="muted">{escape(L["date"])}: {inv.invoice_date}</div></div>')
    due = (inv.total - inv.amount_paid).quantize(Decimal("0.01"))
    rows_sum = [(L["subtotal"], inv.subtotal), (L["vat"], inv.vat_amount),
                (L["total"], inv.total), (L["paid"], inv.amount_paid)]
    summary_rows = "".join(
        f'<div class="row"><span>{escape(lbl)}</span><span class="amt">{val}</span></div>'
        for lbl, val in rows_sum)
    summary = (f'<div class="summary">{summary_rows}'
               f'<div class="row grand"><span>{escape(L["due"])}</span><span class="amt">{due}</span></div></div>')
    return pdf.document(title=L["title"], lang=lang, body=meta + tbl + summary)


def _S(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, dict):
        return {k: _S(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_S(x) for x in v]
    return v


async def _settings(db: AsyncSession) -> Settings:
    return (await db.execute(select(Settings).where(Settings.id == "singleton"))).scalar_one()


@router.get("/customers")
async def list_customers(db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    rows = (await db.execute(select(Customer).order_by(Customer.name))).scalars().all()
    out = []
    for c in rows:
        bal = await ar.customer_open_balance(db, c.id)
        out.append({**CustomerOut.model_validate(c).model_dump(), "open_balance": str(bal)})
    return {"items": _S(out)}


@router.post("/customers", response_model=CustomerOut)
async def create_customer(body: CustomerCreate, db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    c = Customer(name=body.name, phone=body.phone, email=body.email, currency=body.currency,
                 credit_limit=body.credit_limit, notes=body.notes)
    db.add(c)
    await db.commit()
    return CustomerOut.model_validate(c)


@router.get("/invoices")
async def list_invoices(customer_id: str = "", db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    q = select(ARInvoice).order_by(ARInvoice.invoice_date.desc())
    if customer_id:
        q = q.where(ARInvoice.customer_id == customer_id)
    rows = (await db.execute(q)).scalars().all()
    return {"items": [{"id": i.id, "invoice_no": i.invoice_no, "customer_id": i.customer_id,
                       "invoice_date": i.invoice_date, "total": str(i.total), "amount_paid": str(i.amount_paid),
                       "status": i.status.value} for i in rows]}


@router.post("/invoices")
async def create_invoice(body: StandaloneInvoiceCreate, db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_accounting)):
    inv = await ar.post_standalone_invoice(
        db, customer_id=body.customer_id, invoice_date=body.invoice_date, due_date=body.due_date,
        lines=[l.model_dump() for l in body.lines], memo=body.memo, vat_percent=body.vat_percent,
        settings=await _settings(db), actor_user_id=user.id, fx_rate=body.fx_rate)
    await db.commit()
    return {"id": inv.id, "invoice_no": inv.invoice_no, "total": str(inv.total), "status": inv.status.value}


@router.post("/receipts")
async def create_receipt(body: ReceiptCreate, db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_accounting)):
    r = await ar.post_receipt(db, customer_id=body.customer_id, receipt_date=body.receipt_date,
                              amount=body.amount, payment_system_key=body.payment_system_key, memo=body.memo,
                              settings=await _settings(db), actor_user_id=user.id, allocations=body.allocations,
                              currency=body.currency, fx_rate=body.fx_rate)
    await db.commit()
    return {"id": r.id, "receipt_no": r.receipt_no, "unapplied_amount": str(r.unapplied_amount)}


@router.get("/aging")
async def aging(as_of: date, customer_id: str = "", db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    return _S(await ar.compute_aging(db, as_of=as_of, customer_id=customer_id or None))


@router.get("/customers/{customer_id}/statement")
async def statement(customer_id: str, from_date: date = Query(alias="from"), until: date = Query(...),
                    format: str = Query(None), lang: str = Query("en"),
                    db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    data = await ar.customer_statement(db, customer_id, from_date=from_date, until=until)
    if format == "pdf":
        cust = (await db.execute(select(Customer).where(Customer.id == customer_id))).scalar_one_or_none()
        html = _statement_html(data, cust.name if cust else customer_id, lang)
        return pdf.pdf_response(html, filename=f"statement-{customer_id}-{from_date}-{until}")
    return _S(data)


@router.get("/invoices/{invoice_id}/pdf")
async def invoice_pdf(invoice_id: str, lang: str = Query("en"),
                      db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    inv = (await db.execute(select(ARInvoice).where(ARInvoice.id == invoice_id))).scalar_one_or_none()
    if inv is None:
        from fastapi import HTTPException
        raise HTTPException(404, "Invoice not found")
    lines = (await db.execute(
        select(ARInvoiceLine).where(ARInvoiceLine.invoice_id == invoice_id))).scalars().all()
    cust = (await db.execute(select(Customer).where(Customer.id == inv.customer_id))).scalar_one_or_none()
    html = _invoice_html(inv, lines, cust.name if cust else inv.customer_id, lang)
    return pdf.pdf_response(html, filename=f"invoice-{inv.invoice_no}")


@router.get("/verify")
async def verify(db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    return _S(await ar.verify_ar_control(db))
