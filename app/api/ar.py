from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ar
from app.core.permissions import require_accounting
from app.deps import get_db
from app.models import ARInvoice, Customer, Settings, User
from app.schemas.ar import CustomerCreate, CustomerOut, ReceiptCreate, StandaloneInvoiceCreate

router = APIRouter(prefix="/accounting/ar", tags=["accounting-ar"])


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
                    db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    return _S(await ar.customer_statement(db, customer_id, from_date=from_date, until=until))


@router.get("/verify")
async def verify(db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    return _S(await ar.verify_ar_control(db))
