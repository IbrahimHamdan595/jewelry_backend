from datetime import date
from decimal import Decimal
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import expenses, pdf
from app.core.permissions import require_accounting
from app.deps import get_db
from app.models import AccountType, GLAccount, Settings, User, VendorBill, VendorBillLine
from app.schemas.expenses import VendorBillCreate, VendorPaymentCreate

router = APIRouter(prefix="/accounting/expenses", tags=["accounting-expenses"])

_BILL_L = {
    "en": {"title": "Vendor Bill", "vendor": "Vendor", "date": "Date", "no": "Bill No.",
           "desc": "Description", "amount": "Amount", "subtotal": "Subtotal", "vat": "VAT",
           "total": "Total", "paid": "Paid", "due": "Balance due"},
    "ar": {"title": "فاتورة مورّد", "vendor": "المورّد", "date": "التاريخ", "no": "رقم الفاتورة",
           "desc": "الوصف", "amount": "المبلغ", "subtotal": "المجموع الفرعي",
           "vat": "ضريبة القيمة المضافة", "total": "الإجمالي", "paid": "المدفوع", "due": "الرصيد المستحق"},
}


def _bill_html(bill: VendorBill, lines: list[VendorBillLine], lang: str) -> str:
    L = _BILL_L.get(lang, _BILL_L["en"])
    tbl = pdf.table([(L["desc"], False), (L["amount"], True)],
                    [[ln.description, ln.amount] for ln in lines])
    meta = (f'<div class="meta"><div><strong>{escape(L["no"])}:</strong> {escape(bill.bill_no)}</div>'
            f'<div><strong>{escape(L["vendor"])}:</strong> {escape(bill.vendor_name)}</div>'
            f'<div class="muted">{escape(L["date"])}: {bill.bill_date}</div></div>')
    due = (bill.total - bill.amount_paid).quantize(Decimal("0.01"))
    summary_rows = "".join(
        f'<div class="row"><span>{escape(lbl)}</span><span class="amt">{val}</span></div>'
        for lbl, val in [(L["subtotal"], bill.subtotal), (L["vat"], bill.vat_amount),
                         (L["total"], bill.total), (L["paid"], bill.amount_paid)])
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


@router.get("/expense-accounts")
async def expense_accounts(db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    rows = (await db.execute(select(GLAccount).where(GLAccount.type == AccountType.EXPENSE,
                                                     GLAccount.is_active.is_(True)).order_by(GLAccount.code))).scalars().all()
    return {"items": [{"id": a.id, "code": a.code, "name": a.name, "system_key": a.system_key} for a in rows]}


@router.post("/bills")
async def create_bill(body: VendorBillCreate, db: AsyncSession = Depends(get_db), user: User = Depends(require_accounting)):
    bill = await expenses.post_vendor_bill(
        db, vendor_name=body.vendor_name, supplier_id=body.supplier_id, bill_date=body.bill_date,
        due_date=body.due_date, lines=[l.model_dump() for l in body.lines],
        payment_system_key=body.payment_system_key, memo=body.memo, settings=await _settings(db),
        actor_user_id=user.id, tax_code_id=body.tax_code_id, currency=body.currency, fx_rate=body.fx_rate)
    await db.commit()
    return {"id": bill.id, "bill_no": bill.bill_no, "total": str(bill.total), "status": bill.status.value}


@router.get("/bills")
async def list_bills(vendor_name: str = "", db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    q = select(VendorBill).order_by(VendorBill.bill_date.desc())
    if vendor_name:
        q = q.where(VendorBill.vendor_name == vendor_name)
    rows = (await db.execute(q)).scalars().all()
    return {"items": [{"id": b.id, "bill_no": b.bill_no, "vendor_name": b.vendor_name, "bill_date": b.bill_date,
                       "total": str(b.total), "amount_paid": str(b.amount_paid), "status": b.status.value} for b in rows]}


@router.get("/bills/{bill_id}/pdf")
async def bill_pdf(bill_id: str, lang: str = Query("en"),
                   db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    bill = (await db.execute(select(VendorBill).where(VendorBill.id == bill_id))).scalar_one_or_none()
    if bill is None:
        raise HTTPException(404, "Bill not found")
    lines = (await db.execute(
        select(VendorBillLine).where(VendorBillLine.bill_id == bill_id))).scalars().all()
    return pdf.pdf_response(_bill_html(bill, lines, lang), filename=f"bill-{bill.bill_no}")


@router.post("/payments")
async def create_payment(body: VendorPaymentCreate, db: AsyncSession = Depends(get_db), user: User = Depends(require_accounting)):
    p = await expenses.post_vendor_payment(db, vendor_name=body.vendor_name, payment_date=body.payment_date,
        amount=body.amount, payment_system_key=body.payment_system_key, memo=body.memo,
        settings=await _settings(db), actor_user_id=user.id, allocations=body.allocations,
        currency=body.currency, fx_rate=body.fx_rate)
    await db.commit()
    return {"id": p.id, "payment_no": p.payment_no, "unapplied_amount": str(p.unapplied_amount)}


@router.get("/reports/by-category")
async def by_category(from_date: date = Query(alias="from"), until: date = Query(...),
                      db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    return _S(await expenses.expense_by_category(db, from_date=from_date, until=until))


@router.get("/reports/vendor-spend")
async def vendor_spend(from_date: date = Query(alias="from"), until: date = Query(...),
                       db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    return _S(await expenses.vendor_spend(db, from_date=from_date, until=until))


@router.get("/verify")
async def verify(db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    return _S(await expenses.verify_vendor_ap(db))
