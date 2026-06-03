"""Auto-posting bridge (Module 1): operation events → balanced GL entries.

Each `post_*` mapper builds gl.GLLine lists and calls gl.post_entry inside the
caller's transaction (no commit). All mappers are gated by the
`accounting_auto_post_enabled` settings flag and are idempotent on
(source_type, source_id). See the design spec for the posting catalog.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import gl
from app.core.pricing import KARAT_PURITY
from app.models import (
    GLAccount, GLJournalEntry, GLPeriod, PeriodStatus, Settings,
)

ZERO = Decimal("0")
_Q_MONEY = Decimal("0.01")

# source_type strings stored on gl_journal_entry.source_type
SOURCE_ORDER = "ORDER"
SOURCE_ORDER_REFUND = "ORDER_REFUND"
SOURCE_SUPPLIER_PURCHASE = "SUPPLIER_PURCHASE"
SOURCE_SUPPLIER_PAYMENT = "SUPPLIER_PAYMENT"
SOURCE_BUYBACK = "BUYBACK"
SOURCE_MELT = "MELT"
SOURCE_ADJUSTMENT = "ADJUSTMENT"


def auto_post_enabled(settings: Settings) -> bool:
    return bool(getattr(settings, "accounting_auto_post_enabled", False))


async def ensure_period(db: AsyncSession, entry_date: date) -> GLPeriod:
    """Return the month's period; create it OPEN if missing. A CLOSED period is
    returned as-is (gl.post_entry will then hard-fail, which is intended)."""
    period = (
        await db.execute(
            select(GLPeriod).where(
                GLPeriod.year == entry_date.year,
                GLPeriod.period_no == entry_date.month,
            )
        )
    ).scalar_one_or_none()
    if period is None:
        period = GLPeriod(year=entry_date.year, period_no=entry_date.month, status=PeriodStatus.OPEN)
        db.add(period)
        await db.flush()
    return period


async def resolve_account_id(db: AsyncSession, system_key: str) -> str:
    acct = (
        await db.execute(select(GLAccount).where(GLAccount.system_key == system_key))
    ).scalar_one_or_none()
    if acct is None:
        raise HTTPException(status_code=422, detail=f"GL system account {system_key} not seeded")
    return acct.id


async def find_live_entry(db: AsyncSession, source_type: str, source_id: str) -> GLJournalEntry | None:
    """The existing forward (non-reversal) entry for a source, if any. Used for
    idempotency (skip double-posts) and to locate the original to reverse."""
    return (
        await db.execute(
            select(GLJournalEntry).where(
                GLJournalEntry.source_type == source_type,
                GLJournalEntry.source_id == source_id,
                GLJournalEntry.reverses_entry_id.is_(None),
            )
        )
    ).scalars().first()
