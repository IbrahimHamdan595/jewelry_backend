from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core import gl
from app.core.audit_chain import GENESIS_HASH
from app.models import (
    GLAccount, GLJournalChainHead, GLJournalEntry, GLJournalLine, GLPeriod,
    InventoryLedger, AccountType, Denomination, NormalBalance, PeriodStatus,
)

D = Decimal


async def _open_period(db, year=2026, month=6):
    p = GLPeriod(year=year, period_no=month, status=PeriodStatus.OPEN)
    db.add(p)
    await db.flush()
    return p


async def _money_account(db, code, key, normal):
    a = GLAccount(code=code, name=code, type=AccountType.ASSET,
                  denomination=Denomination.MONEY, normal_balance=normal,
                  currency="USD", system_key=key)
    db.add(a)
    await db.flush()
    return a


@pytest.mark.asyncio
async def test_next_entry_no_increments_per_day(db):
    n1 = await gl._next_entry_no(db, date(2026, 6, 3))
    n2 = await gl._next_entry_no(db, date(2026, 6, 3))
    n3 = await gl._next_entry_no(db, date(2026, 6, 4))
    assert n1 == "JE-20260603-001"
    assert n2 == "JE-20260603-002"
    assert n3 == "JE-20260604-001"


@pytest.mark.asyncio
async def test_resolve_open_period_ok_and_missing_and_closed(db):
    p = await _open_period(db)
    got = await gl._resolve_open_period(db, date(2026, 6, 15))
    assert got.id == p.id

    with pytest.raises(Exception):
        await gl._resolve_open_period(db, date(2026, 7, 1))  # no July period

    p.status = PeriodStatus.CLOSED
    await db.flush()
    with pytest.raises(Exception):
        await gl._resolve_open_period(db, date(2026, 6, 15))


@pytest.mark.asyncio
async def test_post_entry_balanced_advances_chain_and_audits(db):
    await _open_period(db)
    cash = await _money_account(db, "1000", "CASH", NormalBalance.DEBIT)
    rev = await _money_account(db, "4000", "SALES_REVENUE", NormalBalance.CREDIT)

    entry = await gl.post_entry(
        db, entry_date=date(2026, 6, 3), memo="cash sale",
        source_type=gl.SOURCE_MANUAL, source_id=None, actor_user_id="u1",
        lines=[
            gl.GLLine(account_id=cash.id, denomination="MONEY", base_debit=D("100"), money_debit=D("100")),
            gl.GLLine(account_id=rev.id, denomination="MONEY", base_credit=D("100"), money_credit=D("100")),
        ],
    )
    assert entry.entry_no == "JE-20260603-001"
    assert entry.prev_hash == GENESIS_HASH and len(entry.entry_hash) == 64

    head = (await db.execute(select(GLJournalChainHead).where(GLJournalChainHead.id == 1))).scalar_one()
    assert head.row_count == 1 and head.latest_entry_hash == entry.entry_hash

    audit = (await db.execute(select(InventoryLedger).where(InventoryLedger.ref_id == entry.id))).scalar_one()
    assert audit.event_type == "GL_ENTRY_POSTED"


@pytest.mark.asyncio
async def test_post_entry_unbalanced_rejected(db):
    await _open_period(db)
    cash = await _money_account(db, "1000", "CASH", NormalBalance.DEBIT)
    rev = await _money_account(db, "4000", "SALES_REVENUE", NormalBalance.CREDIT)
    with pytest.raises(Exception):
        await gl.post_entry(
            db, entry_date=date(2026, 6, 3), memo="bad", source_type=gl.SOURCE_MANUAL,
            source_id=None, actor_user_id="u1",
            lines=[
                gl.GLLine(account_id=cash.id, denomination="MONEY", base_debit=D("100")),
                gl.GLLine(account_id=rev.id, denomination="MONEY", base_credit=D("90")),
            ],
        )


@pytest.mark.asyncio
async def test_post_entry_closed_period_rejected(db):
    p = await _open_period(db)
    p.status = PeriodStatus.CLOSED
    await db.flush()
    cash = await _money_account(db, "1000", "CASH", NormalBalance.DEBIT)
    rev = await _money_account(db, "4000", "SALES_REVENUE", NormalBalance.CREDIT)
    with pytest.raises(Exception):
        await gl.post_entry(
            db, entry_date=date(2026, 6, 3), memo="x", source_type=gl.SOURCE_MANUAL,
            source_id=None, actor_user_id="u1",
            lines=[
                gl.GLLine(account_id=cash.id, denomination="MONEY", base_debit=D("100")),
                gl.GLLine(account_id=rev.id, denomination="MONEY", base_credit=D("100")),
            ],
        )


@pytest.mark.asyncio
async def test_post_entry_denomination_from_db_overrides_caller(db):
    """Engine must read denomination from the account, not trust the caller."""
    await _open_period(db)
    cash = await _money_account(db, "1000", "CASH", NormalBalance.DEBIT)
    rev = await _money_account(db, "4000", "SALES_REVENUE", NormalBalance.CREDIT)
    # Caller lies (says DUAL + metal) on a MONEY account → must be rejected.
    with pytest.raises(Exception):
        await gl.post_entry(
            db, entry_date=date(2026, 6, 3), memo="x", source_type=gl.SOURCE_MANUAL,
            source_id=None, actor_user_id="u1",
            lines=[
                gl.GLLine(account_id=cash.id, denomination="DUAL", base_debit=D("100"),
                          metal_debit_grams=D("5"), karat="K21"),
                gl.GLLine(account_id=rev.id, denomination="MONEY", base_credit=D("100")),
            ],
        )


@pytest.mark.asyncio
async def test_reverse_entry_swaps_and_nets_zero(db):
    await _open_period(db)
    cash = await _money_account(db, "1000", "CASH", NormalBalance.DEBIT)
    rev = await _money_account(db, "4000", "SALES_REVENUE", NormalBalance.CREDIT)
    orig = await gl.post_entry(
        db, entry_date=date(2026, 6, 3), memo="sale", source_type=gl.SOURCE_MANUAL,
        source_id="ord-1", actor_user_id="u1",
        lines=[
            gl.GLLine(account_id=cash.id, denomination="MONEY", base_debit=D("100"), money_debit=D("100")),
            gl.GLLine(account_id=rev.id, denomination="MONEY", base_credit=D("100"), money_credit=D("100")),
        ],
    )
    rev_entry = await gl.reverse_entry(db, original_entry_id=orig.id, actor_user_id="u1",
                                       entry_date=date(2026, 6, 3), memo="void sale")
    assert rev_entry.reverses_entry_id == orig.id

    # Net of original + reversal is zero on both accounts.
    lines = (await db.execute(select(GLJournalLine))).scalars().all()
    net_cash = sum((l.base_debit - l.base_credit for l in lines if l.account_id == cash.id), D("0"))
    net_rev = sum((l.base_debit - l.base_credit for l in lines if l.account_id == rev.id), D("0"))
    assert net_cash == D("0") and net_rev == D("0")
