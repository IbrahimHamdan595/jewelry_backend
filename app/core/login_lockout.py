"""Per-account login lockout (NEX-47).

The per-IP limit on /auth/login (`app/core/rate_limit.py`) does nothing
against a guess spread over many addresses. This is the second layer: after
LOCKOUT_THRESHOLD consecutive failed logins inside LOCKOUT_WINDOW the claimed
email is refused for LOCKOUT_DURATION, whatever password is presented.

NO NEW STATE
------------
There is no counter column and no lockout table. `auth_audit_log` already
holds one LOGIN_FAILED row per wrong password, with the email that was
claimed, so both questions are answered from it:

  • locked?   an ACCOUNT_LOCKED row for this email newer than LOCKOUT_DURATION
  • how many? LOGIN_FAILED rows for this email inside LOCKOUT_WINDOW that are
              newer than its last LOGIN_SUCCESS / ACCOUNT_LOCKED row

A successful login therefore resets the count, and so does the lock itself:
when it expires the email starts again from zero. Requests refused while the
lock holds write nothing, so hammering a locked account cannot extend the
lock — it always ends LOCKOUT_DURATION after the attempt that triggered it.

NO ACCOUNT-EXISTENCE ORACLE
---------------------------
Everything is keyed on the email that was CLAIMED (lower-cased, so changing
the capitalisation does not buy another ten guesses), never on a user row.
An address with no account behind it collects the same rows, is locked at
the same attempt and gets the same response as a real one.

WHY THE FAILURE ROW IS WRITTEN INLINE
-------------------------------------
Every other auth event is written by a fire-and-forget background task
(`fire_auth_event`). Counting from rows written that way is not safe under
load: the task needs a pool connection, and a flood of login requests — the
attack this exists for — queues ahead of it, so the rows that should trip
the lock arrive late or time out and never arrive. `record_failed_login`
therefore writes on the request's own session, which already holds a
connection, before the 401 is returned. The count it then reads includes
the row it just wrote and — because every writer holds the chain-head lock —
every failure committed before it, so racing requests cannot each decide
they were the tenth: one ACCOUNT_LOCKED row per ten consecutive failures.

What remains is bounded by concurrency, not by time: requests that passed
`is_locked` before the lock row committed still get their one attempt, so a
burst can exceed the threshold by at most the number of logins in flight,
which the DB pool caps (15 connections today). LOGIN_SUCCESS is still
written in the background; a late one only delays a reset, which errs on
the strict side.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_audit import (
    EVENT_ACCOUNT_LOCKED,
    EVENT_LOGIN_FAILED,
    EVENT_LOGIN_SUCCESS,
    append_auth_event,
)
from app.models import AuthAuditLog

log = logging.getLogger(__name__)

# A shop terminal mistyping a password a few times a day never comes close:
# it takes ten wrong passwords in a row, with no successful login in between,
# all inside fifteen minutes.
LOCKOUT_THRESHOLD = 10
LOCKOUT_WINDOW = timedelta(minutes=15)
LOCKOUT_DURATION = timedelta(minutes=15)

# Deliberately says nothing about the account, the count or the time left.
LOCKOUT_DETAIL = "Too many failed login attempts. Try again later."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalise_email(email: str) -> str:
    return email.strip().lower()


def _claimed(email: str):
    return func.lower(AuthAuditLog.claimed_email) == normalise_email(email)


async def is_locked(db: AsyncSession, email: str) -> bool:
    """True while a lockout triggered in the last LOCKOUT_DURATION holds."""
    row = (
        await db.execute(
            select(AuthAuditLog.id)
            .where(
                _claimed(email),
                AuthAuditLog.event_type == EVENT_ACCOUNT_LOCKED,
                AuthAuditLog.occurred_at > _now() - LOCKOUT_DURATION,
            )
            .limit(1)
        )
    ).first()
    return row is not None


async def _consecutive_failures(db: AsyncSession, email: str) -> int:
    """Failed logins in the window since the email's last success or lock."""
    window_start = _now() - LOCKOUT_WINDOW
    last_reset = (
        select(func.max(AuthAuditLog.occurred_at))
        .where(
            _claimed(email),
            AuthAuditLog.event_type.in_((EVENT_LOGIN_SUCCESS, EVENT_ACCOUNT_LOCKED)),
            AuthAuditLog.occurred_at > window_start,
        )
        .scalar_subquery()
    )
    return (
        await db.execute(
            select(func.count())
            .select_from(AuthAuditLog)
            .where(
                _claimed(email),
                AuthAuditLog.event_type == EVENT_LOGIN_FAILED,
                AuthAuditLog.occurred_at > window_start,
                or_(last_reset.is_(None), AuthAuditLog.occurred_at > last_reset),
            )
        )
    ).scalar_one()


async def record_failed_login(
    db: AsyncSession,
    *,
    claimed_email: str,
    client_ip: str | None,
    user_agent: str | None,
) -> None:
    """Audit a wrong-password login and lock the email if it was one too many.

    Runs on the request's session and commits it. Never raises: like every
    auth-audit write this is best effort, and a recorder failure must not
    turn the 401 the caller is about to send into a 500. (A failure here
    means that one attempt goes uncounted; it is logged.)
    """
    try:
        await append_auth_event(
            db,
            event_type=EVENT_LOGIN_FAILED,
            user_id=None,                # email is claimed-but-unverified
            claimed_email=claimed_email,
            client_ip=client_ip,
            user_agent=user_agent,
            detail="invalid credentials",
        )
        await db.flush()  # the count below must include the row just added
        if await _consecutive_failures(db, claimed_email) >= LOCKOUT_THRESHOLD:
            await append_auth_event(
                db,
                event_type=EVENT_ACCOUNT_LOCKED,
                user_id=None,            # same row whether or not the account exists
                claimed_email=claimed_email,
                client_ip=client_ip,
                user_agent=user_agent,
                detail=(
                    f"{LOCKOUT_THRESHOLD} consecutive failed logins; locked for "
                    f"{int(LOCKOUT_DURATION.total_seconds() // 60)} minutes"
                ),
            )
        await db.commit()
    except Exception:
        log.exception("failed-login audit write failed (claimed_email=%s)", claimed_email)
        try:
            await db.rollback()
        except Exception:
            log.exception("rollback after failed-login audit write failed")
