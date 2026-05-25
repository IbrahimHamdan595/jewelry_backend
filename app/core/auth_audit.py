"""Best-effort auth-audit recorder.

AUDIT RATIONALE
---------------
Auth events (login success/failure, logout, password change) write to
`auth_audit_log`, a dedicated table with its own hash chain. Architecture
notes that make this MODULE different from `app/core/ledger.py`:

  1. Writes are BEST EFFORT, not transactionally bound to the auth action.
     If the recorder fails (DB down, chain head locked too long, whatever),
     the user MUST still be able to log in or out. A logging failure must
     never roll back a successful authentication.
     Implementation: `record_auth_event_safe()` opens its OWN DB session
     (not the request session) and swallows every exception, logging it
     for ops visibility. Designed to be called from `BackgroundTasks` so
     the auth response is sent before the audit write even starts.

  2. Failed-login bursts must not slow the real login path. Because the
     write runs in a background task on a fresh session, contention on
     the auth_audit_chain_head row is invisible to the user — their
     login response has already gone out.

  3. Failed logins are unauthenticated. The submitted email is recorded
     as `claimed_email` (with NO foreign key to users) and `user_id` is
     NULL. The audit row captures what was submitted, not a verified
     identity — important for spotting probes against non-existent
     accounts.

  4. Client IP comes from `get_client_ip()` (this module) which reads
     `X-Forwarded-For` first because Render terminates TLS at its
     load balancer and `request.client.host` would otherwise be the
     proxy's internal IP.

This module deliberately does NOT mirror `record()` from `app/core/ledger.py`:
that function is synchronous-within-a-transaction and would defeat the
best-effort semantics above. Do not "consolidate" the two helpers.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import Request
from sqlalchemy import select

from app.config import settings
from app.core.audit_chain import compute_auth_entry_hash
from app.db.session import async_session_factory
from app.models import AuthAuditChainHead, AuthAuditLog

log = logging.getLogger(__name__)


# Event type names — string constants so callers don't typo and so the
# read-side filter UI can present a known set.
EVENT_LOGIN_SUCCESS = "LOGIN_SUCCESS"
EVENT_LOGIN_FAILED = "LOGIN_FAILED"
EVENT_LOGOUT = "LOGOUT"
EVENT_PASSWORD_CHANGED = "PASSWORD_CHANGED"


def get_client_ip(request: Request) -> str | None:
    """Return the best-effort real client IP.

    Render (and most modern PaaS) terminates TLS at a load balancer and
    forwards traffic to the app over an internal network. As a result,
    `request.client.host` is the LB's internal address — useless for
    auditing who actually hit the endpoint.

    Render sets `X-Forwarded-For` on every request reaching the app. The
    header has the form "<original-client>, <proxy1>, <proxy2>, ..." —
    the LEFTMOST entry is the original client.

    We trust this header HERE because:
      • The only path traffic can reach this app on Render is via Render's
        own ingress, which strips any client-supplied X-Forwarded-For and
        sets its own. A client cannot spoof the header in production.
      • In local dev, `request.client.host` is correct and the header is
        usually absent — the fallback covers that.

    DO NOT copy this helper for other purposes without re-evaluating the
    spoof-trust argument; an endpoint that returns its result to the user
    would create an injection vector if XFF were honored from an untrusted
    network.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # Leftmost is the original client. Subsequent entries are proxy hops.
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else None


def _retention_until(now: datetime) -> datetime:
    return now + timedelta(days=settings.auth_audit_retention_days)


def fire_auth_event(
    *,
    event_type: str,
    user_id: str | None,
    claimed_email: str | None,
    client_ip: str | None,
    user_agent: str | None,
    detail: str | None = None,
) -> None:
    """Schedule an auth-audit write to run on the event loop without awaiting it.

    DELIBERATELY NOT using FastAPI's `BackgroundTasks` parameter: those only
    execute when the endpoint returns successfully. Raising `HTTPException`
    bypasses them — which would silently drop the most important audit
    events (failed logins). `asyncio.create_task` schedules the coroutine
    on the running event loop regardless of whether the request handler
    returns or raises.

    The recorder itself is wrapped in try/except (best-effort), so the
    created task can never propagate an exception back into the event loop.
    """
    asyncio.create_task(
        record_auth_event_safe(
            event_type=event_type,
            user_id=user_id,
            claimed_email=claimed_email,
            client_ip=client_ip,
            user_agent=user_agent,
            detail=detail,
        )
    )


async def record_auth_event_safe(
    *,
    event_type: str,
    user_id: str | None,
    claimed_email: str | None,
    client_ip: str | None,
    user_agent: str | None,
    detail: str | None = None,
) -> None:
    """Write an auth-audit row in its own session. Never raises.

    Best-effort by design: any exception (DB unavailable, chain head
    contention, schema drift) is logged and swallowed. The caller has
    already returned a response to the user and must not be affected
    by audit-side failures.

    Usually invoked indirectly via `fire_auth_event(...)`, which schedules
    this coroutine on the event loop. Safe to call inline if you have a
    reason to await it, but doing so reintroduces audit-side latency.
    """
    try:
        async with async_session_factory() as session:
            head = (
                await session.execute(
                    select(AuthAuditChainHead)
                    .where(AuthAuditChainHead.id == 1)
                    .with_for_update()
                )
            ).scalar_one()

            occurred_at = datetime.now(timezone.utc)
            fields: dict[str, Any] = {
                "event_type": event_type,
                "occurred_at": occurred_at,
                "user_id": user_id,
                "claimed_email": claimed_email,
                "client_ip": client_ip,
                "user_agent": user_agent,
                "detail": detail,
            }
            entry_hash = compute_auth_entry_hash(
                prev_hash=head.latest_entry_hash,
                fields=fields,
            )

            row = AuthAuditLog(
                id=uuid4().hex,
                event_type=event_type,
                occurred_at=occurred_at,
                user_id=user_id,
                claimed_email=claimed_email,
                client_ip=client_ip,
                user_agent=user_agent,
                detail=detail,
                retention_until_at=_retention_until(occurred_at),
                prev_hash=head.latest_entry_hash,
                entry_hash=entry_hash,
            )
            session.add(row)

            head.latest_entry_hash = entry_hash
            head.row_count = head.row_count + 1

            await session.commit()
    except Exception:
        # Logged but never re-raised. The auth path has already returned
        # to the user; an audit failure must not break authentication.
        log.exception(
            "auth_audit write failed (event_type=%s, claimed_email=%s)",
            event_type,
            claimed_email,
        )
