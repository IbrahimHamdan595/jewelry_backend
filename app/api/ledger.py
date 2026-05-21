from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import require_admin
from app.deps import get_db
from app.models import InventoryLedger, User
from app.schemas.ledger import LedgerEntryOut, LedgerListOut

router = APIRouter(prefix="/ledger", tags=["ledger"])


@router.get("", response_model=LedgerListOut)
async def list_ledger(
    event_type: str = "",
    ref_type: str = "",
    ref_id: str = "",
    since: datetime | None = None,
    until: datetime | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    q = select(InventoryLedger)
    if event_type:
        q = q.where(InventoryLedger.event_type == event_type)
    if ref_type:
        q = q.where(InventoryLedger.ref_type == ref_type)
    if ref_id:
        q = q.where(InventoryLedger.ref_id == ref_id)
    if since:
        q = q.where(InventoryLedger.occurred_at >= since)
    if until:
        q = q.where(InventoryLedger.occurred_at <= until)

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()

    q = q.order_by(InventoryLedger.occurred_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(q)).scalars().all()

    return LedgerListOut(
        items=[LedgerEntryOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )
