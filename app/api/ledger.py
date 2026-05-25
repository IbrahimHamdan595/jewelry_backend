from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_chain import verify_chain
from app.core.permissions import require_admin
from app.deps import get_db
from app.models import InventoryLedger, InventoryLedgerChainHead, User
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


@router.get("/verify")
async def verify_ledger(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Walk the full hash chain and report tamper detection.

    AUDIT: this is the verification endpoint for the InventoryLedger chain.
    Any successful UPDATE or DELETE against `inventory_ledger` (which the
    application no longer performs and which audit phase A2 will additionally
    block at the DB layer) breaks the chain at exactly the affected row.

    Returns
    -------
    {
      "status": "intact" | "broken" | "empty",
      "total_rows": int,
      "head_row_count": int,      # what the chain-head table claims
      "head_latest_hash": str,    # ...vs what we actually computed
      "computed_latest_hash": str,
      "head_matches": bool,       # head agrees with recompute
      "first_break": {...} | None # populated if status == "broken"
    }

    Cost is O(n) where n is the ledger size. Acceptable at current scale;
    pagination/streaming can be added if the table grows past tens of
    millions of rows.
    """
    rows = (
        await db.execute(
            select(InventoryLedger).order_by(
                InventoryLedger.occurred_at, InventoryLedger.id
            )
        )
    ).scalars().all()

    row_dicts = [
        {
            "id": r.id,
            "prev_hash": r.prev_hash,
            "entry_hash": r.entry_hash,
            "event_type": r.event_type,
            "actor_user_id": r.actor_user_id,
            "occurred_at": r.occurred_at,
            "ref_type": r.ref_type,
            "ref_id": r.ref_id,
            "payload": r.payload,
        }
        for r in rows
    ]
    result = verify_chain(row_dicts)

    head = (
        await db.execute(
            select(InventoryLedgerChainHead).where(InventoryLedgerChainHead.id == 1)
        )
    ).scalar_one()

    computed_latest = rows[-1].entry_hash if rows else "GENESIS"
    return {
        **result,
        "head_row_count": head.row_count,
        "head_latest_hash": head.latest_entry_hash,
        "computed_latest_hash": computed_latest,
        "head_matches": (
            head.latest_entry_hash == computed_latest and head.row_count == len(rows)
        ),
    }
