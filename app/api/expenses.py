from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import expenses
from app.core.permissions import require_accounting
from app.deps import get_db
from app.models import AccountType, GLAccount, Settings, User, VendorBill
from app.schemas.expenses import VendorBillCreate, VendorPaymentCreate

router = APIRouter(prefix="/accounting/expenses", tags=["accounting-expenses"])


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
