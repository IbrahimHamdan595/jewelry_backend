from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ap
from app.core.permissions import require_accounting
from app.deps import get_db
from app.models import DebtUnit, Supplier, SupplierBalance, User

router = APIRouter(prefix="/accounting/ap", tags=["accounting-ap"])


def _S(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, dict):
        return {k: _S(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_S(x) for x in v]
    return v


@router.get("/verify")
async def verify(db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    return _S(await ap.verify_ap_control(db))


@router.get("/aging")
async def aging(as_of: date, db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    return _S(await ap.compute_ap_aging(db, as_of=as_of))


@router.get("/suppliers/{supplier_id}/statement")
async def statement(supplier_id: str, from_date: date = Query(alias="from"), until: date = Query(...),
                    db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    return _S(await ap.supplier_statement(db, supplier_id, from_date=from_date, until=until))


@router.get("/balances")
async def balances(db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    suppliers = (await db.execute(select(Supplier).order_by(Supplier.name))).scalars().all()
    out = []
    for s in suppliers:
        rows = (await db.execute(select(SupplierBalance).where(SupplierBalance.supplier_id == s.id))).scalars().all()
        cash = sum((r.balance for r in rows if r.unit == DebtUnit.CASH), Decimal("0"))
        gold = {r.karat: str(r.balance) for r in rows if r.unit == DebtUnit.GOLD and r.balance != 0}
        out.append({"id": s.id, "name": s.name, "cash_owed": str(cash), "gold_owed_by_karat": gold})
    return {"suppliers": out}
