from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import tax
from app.core.permissions import require_accounting
from app.deps import get_db
from app.models import TaxCode, User

router = APIRouter(prefix="/accounting/tax", tags=["accounting-tax"])


def _S(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, dict):
        return {k: _S(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_S(x) for x in v]
    return v


@router.post("/seed-codes")
async def seed_codes(db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    created = await tax.seed_tax_codes(db)
    await db.commit()
    return {"created": created}


@router.get("/codes")
async def list_codes(db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    rows = (await db.execute(select(TaxCode).order_by(TaxCode.code))).scalars().all()
    return {"items": [{"id": c.id, "code": c.code, "name": c.name, "rate": str(c.rate), "is_active": c.is_active} for c in rows]}


@router.post("/codes")
async def create_code(body: dict, db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    c = TaxCode(code=body["code"], name=body.get("name", body["code"]), rate=Decimal(str(body.get("rate", "0"))))
    db.add(c)
    await db.commit()
    return {"id": c.id, "code": c.code, "rate": str(c.rate)}


@router.patch("/codes/{code_id}")
async def update_code(code_id: str, body: dict, db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    c = (await db.execute(select(TaxCode).where(TaxCode.id == code_id))).scalar_one_or_none()
    if c is None:
        raise HTTPException(404, "Tax code not found")
    if "rate" in body:
        c.rate = Decimal(str(body["rate"]))
    if "name" in body:
        c.name = body["name"]
    if "is_active" in body:
        c.is_active = bool(body["is_active"])
    await db.commit()
    return {"id": c.id, "code": c.code, "rate": str(c.rate), "is_active": c.is_active}


@router.get("/vat-return")
async def vat_return(year: int, quarter: int = Query(ge=1, le=4),
                     db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    return _S(await tax.compute_vat_return(db, year=year, quarter=quarter))
