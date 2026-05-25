"""Read-only endpoints for the auth audit log (audit phase A3b).

Admin-only. List + verify chain. No write endpoint here — auth events are
written exclusively by `record_auth_event_safe()` from the auth flows
themselves (via BackgroundTasks).
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_chain import verify_auth_chain
from app.core.permissions import require_admin
from app.deps import get_db
from app.models import AuthAuditChainHead, AuthAuditLog, User

router = APIRouter(prefix="/auth-audit", tags=["auth-audit"])


@router.get("")
async def list_auth_audit(
    event_type: str = "",
    user_id: str = "",
    claimed_email: str = "",
    client_ip: str = "",
    since: datetime | None = None,
    until: datetime | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """List auth audit rows, newest first. Paged. Admin-only."""
    q = select(AuthAuditLog)
    if event_type:
        q = q.where(AuthAuditLog.event_type == event_type)
    if user_id:
        q = q.where(AuthAuditLog.user_id == user_id)
    if claimed_email:
        q = q.where(AuthAuditLog.claimed_email == claimed_email)
    if client_ip:
        q = q.where(AuthAuditLog.client_ip == client_ip)
    if since:
        q = q.where(AuthAuditLog.occurred_at >= since)
    if until:
        q = q.where(AuthAuditLog.occurred_at <= until)

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()

    q = q.order_by(AuthAuditLog.occurred_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(q)).scalars().all()

    return {
        "items": [
            {
                "id": r.id,
                "event_type": r.event_type,
                "occurred_at": r.occurred_at.isoformat(),
                "user_id": r.user_id,
                "claimed_email": r.claimed_email,
                "client_ip": r.client_ip,
                "user_agent": r.user_agent,
                "detail": r.detail,
                "retention_until_at": r.retention_until_at.isoformat(),
                # Chain pointers exposed for auditor inspection; integrity is
                # checked via /verify rather than per-row recompute here.
                "prev_hash": r.prev_hash,
                "entry_hash": r.entry_hash,
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/verify")
async def verify_auth_audit(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Walk the auth-audit chain and report tamper detection.

    Same response shape as `/api/ledger/verify` but for the auth chain.
    O(n) over the full table; pagination/streaming can be added if volume
    grows past tens of millions of rows.
    """
    rows = (
        await db.execute(
            select(AuthAuditLog).order_by(
                AuthAuditLog.occurred_at, AuthAuditLog.id
            )
        )
    ).scalars().all()

    row_dicts = [
        {
            "id": r.id,
            "prev_hash": r.prev_hash,
            "entry_hash": r.entry_hash,
            "event_type": r.event_type,
            "occurred_at": r.occurred_at,
            "user_id": r.user_id,
            "claimed_email": r.claimed_email,
            "client_ip": r.client_ip,
            "user_agent": r.user_agent,
            "detail": r.detail,
        }
        for r in rows
    ]
    result = verify_auth_chain(row_dicts)

    head = (
        await db.execute(
            select(AuthAuditChainHead).where(AuthAuditChainHead.id == 1)
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
