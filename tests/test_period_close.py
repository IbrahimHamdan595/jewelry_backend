from datetime import date
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.core import gl, period_close
from app.core.coa_seed import seed_chart_of_accounts
from app.core.gl import compute_trial_balance
from app.models import (
    BankAccount,
    BankStatementLine,
    GLAccount,
    GLPeriod,
    PeriodStatus,
    VendorBill,
    VendorBillStatus,
)


async def _acct(db, system_key: str) -> str:
    return (await db.execute(
        select(GLAccount.id).where(GLAccount.system_key == system_key))).scalar_one()


async def _seed(db, months=(1, 6, 12)):
    await seed_chart_of_accounts(db)
    from app.core.bank import adopt_seeded_accounts
    await adopt_seeded_accounts(db)
    for m in months:
        db.add(GLPeriod(year=2026, period_no=m, status=PeriodStatus.OPEN))
    await db.flush()


def _m(account_id, *, debit=D("0"), credit=D("0")):
    return gl.GLLine(account_id=account_id, denomination="MONEY",
                     base_debit=debit, base_credit=credit,
                     money_debit=debit, money_credit=credit, currency="USD")


async def _post(db, entry_date, lines, source_type="TEST"):
    return await gl.post_entry(db, entry_date=entry_date, memo="t", source_type=source_type,
                               source_id=None, lines=lines, actor_user_id="u1")


async def _close_all_2026_months(db):
    for p in (await db.execute(select(GLPeriod).where(GLPeriod.year == 2026))).scalars().all():
        p.status = PeriodStatus.CLOSED
    await db.flush()


# ── Task 1: closed-period exemption + event constant ──────────────────────────

@pytest.mark.asyncio
async def test_post_into_closed_period_rejected_by_default(db):
    await _seed(db, months=(6,))
    cash = await _acct(db, "CASH")
    obe = await _acct(db, "OPENING_BALANCE_EQUITY")
    p = (await db.execute(select(GLPeriod).where(GLPeriod.period_no == 6))).scalar_one()
    p.status = PeriodStatus.CLOSED
    await db.flush()
    with pytest.raises(Exception) as ei:
        await gl.post_entry(db, entry_date=date(2026, 6, 10), memo="x", source_type="TEST",
                            source_id=None, lines=[_m(cash, debit=D("10")), _m(obe, credit=D("10"))],
                            actor_user_id="u1")
    assert "CLOSED" in str(ei.value)


@pytest.mark.asyncio
async def test_allow_closed_period_permits_post(db):
    await _seed(db, months=(6,))
    cash = await _acct(db, "CASH")
    obe = await _acct(db, "OPENING_BALANCE_EQUITY")
    p = (await db.execute(select(GLPeriod).where(GLPeriod.period_no == 6))).scalar_one()
    p.status = PeriodStatus.CLOSED
    await db.flush()
    entry = await gl.post_entry(db, entry_date=date(2026, 6, 10), memo="close-only", source_type="YEAR_CLOSE",
                                source_id=None, lines=[_m(cash, debit=D("10")), _m(obe, credit=D("10"))],
                                actor_user_id="u1", allow_closed_period=True)
    assert entry.id and entry.source_type == "YEAR_CLOSE"


def test_year_closed_event_constant():
    from app.core import ledger
    assert ledger.EVENT_GL_YEAR_CLOSED == "GL_YEAR_CLOSED"


# ── Task 2: readiness ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_readiness_blocks_out_of_order_and_warns_soft(db):
    await _seed(db, months=(5, 6))
    ba = (await db.execute(select(BankAccount))).scalars().first()
    db.add(BankStatementLine(bank_account_id=ba.id, stmt_date=date(2026, 6, 15),
                             description="x", amount=D("100"), matched_gl_line_id=None))
    db.add(VendorBill(bill_no="BILL-T1", vendor_name="V", bill_date=date(2026, 6, 20), total=D("50"),
                      subtotal=D("50"), status=VendorBillStatus.OPEN))
    await db.flush()

    r = await period_close.close_readiness(db, year=2026, period_no=6)
    assert r["can_close"] is False
    hard = {h["key"]: h for h in r["hard"]}
    assert hard["prior_periods_closed"]["ok"] is False
    assert hard["chain_intact"]["ok"] is True
    soft = {s["key"]: s for s in r["soft"]}
    assert soft["unreconciled_bank_lines"]["count"] == 1
    assert soft["open_vendor_bills"]["count"] == 1


@pytest.mark.asyncio
async def test_readiness_ok_when_prior_closed(db):
    await _seed(db, months=(5, 6))
    may = (await db.execute(select(GLPeriod).where(GLPeriod.period_no == 5))).scalar_one()
    may.status = PeriodStatus.CLOSED
    await db.flush()
    r = await period_close.close_readiness(db, year=2026, period_no=6)
    assert r["can_close"] is True


# ── Task 3: year-close preview ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_year_close_preview_nets_income(db):
    await _seed(db, months=(6,))
    cash = await _acct(db, "CASH")
    rev = await _acct(db, "SALES_REVENUE")
    rent = await _acct(db, "RENT_EXPENSE")
    await _post(db, date(2026, 6, 5), [_m(cash, debit=D("1000")), _m(rev, credit=D("1000"))])
    await _post(db, date(2026, 6, 6), [_m(rent, debit=D("300")), _m(cash, credit=D("300"))])

    pv = await period_close.year_close_preview(db, year=2026)
    assert pv["net_income"] == D("700.00")
    assert pv["already_closed"] is False
    re_line = [l for l in pv["lines"] if l["system_key"] == "RETAINED_EARNINGS"][0]
    assert re_line["credit"] == D("700.00")


# ── Task 4: close_year ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_close_year_zeroes_pnl_money_keeps_metal(db):
    await _seed(db, months=(6,))
    cash = await _acct(db, "CASH")
    rev = await _acct(db, "SALES_REVENUE")
    cogs = await _acct(db, "METAL_COGS")
    metal_inv = await _acct(db, "METAL_INVENTORY")
    rent = await _acct(db, "RENT_EXPENSE")
    obe = await _acct(db, "OPENING_BALANCE_EQUITY")

    await _post(db, date(2026, 6, 1), [
        gl.GLLine(account_id=metal_inv, denomination="DUAL", base_debit=D("4000"),
                  money_debit=D("4000"), metal_debit_grams=D("100"), karat="K21", currency="USD"),
        gl.GLLine(account_id=obe, denomination="DUAL", base_credit=D("4000"),
                  money_credit=D("4000"), metal_credit_grams=D("100"), karat="K21", currency="USD"),
    ])
    await _post(db, date(2026, 6, 5), [_m(cash, debit=D("1000")), _m(rev, credit=D("1000"))])
    await _post(db, date(2026, 6, 5), [
        gl.GLLine(account_id=cogs, denomination="DUAL", base_debit=D("600"),
                  metal_debit_grams=D("40"), karat="K21", currency="USD"),
        gl.GLLine(account_id=metal_inv, denomination="DUAL", base_credit=D("600"),
                  metal_credit_grams=D("40"), karat="K21", currency="USD"),
    ])
    await _post(db, date(2026, 6, 7), [_m(rent, debit=D("100")), _m(cash, credit=D("100"))])

    await _close_all_2026_months(db)
    entry = await period_close.close_year(db, year=2026, actor_user_id="admin")
    assert entry.source_type == "YEAR_CLOSE"

    tb = await compute_trial_balance(db, as_of=date(2026, 12, 31))
    accts = {a["system_key"]: a for a in tb["accounts"] if a["system_key"]}
    assert accts["SALES_REVENUE"]["net_base"] == D("0.00")
    assert accts["METAL_COGS"]["net_base"] == D("0.00")
    assert accts["RENT_EXPENSE"]["net_base"] == D("0.00")
    assert accts["RETAINED_EARNINGS"]["base_credit"] - accts["RETAINED_EARNINGS"]["base_debit"] == D("300.00")
    assert accts["METAL_COGS"]["metal_by_karat"]["K21"]["net_grams"] == D("40.000")
    assert tb["balanced"] is True and tb["metal_balanced"] is True


@pytest.mark.asyncio
async def test_close_year_idempotent_and_autoopens_next_year(db):
    await _seed(db, months=(6,))
    cash = await _acct(db, "CASH")
    rev = await _acct(db, "SALES_REVENUE")
    await _post(db, date(2026, 6, 5), [_m(cash, debit=D("1000")), _m(rev, credit=D("1000"))])
    await _close_all_2026_months(db)

    await period_close.close_year(db, year=2026, actor_user_id="admin")
    next_periods = (await db.execute(
        select(GLPeriod).where(GLPeriod.year == 2027))).scalars().all()
    assert len(next_periods) == 12 and all(p.status == PeriodStatus.OPEN for p in next_periods)
    with pytest.raises(Exception) as ei:
        await period_close.close_year(db, year=2026, actor_user_id="admin")
    assert "already closed" in str(ei.value).lower()


async def _closed_2026(db):
    """Revenue 1000, rent 300, every 2026 month closed, year closed."""
    await _seed(db, months=(6,))
    cash = await _acct(db, "CASH")
    rev = await _acct(db, "SALES_REVENUE")
    rent = await _acct(db, "RENT_EXPENSE")
    await _post(db, date(2026, 6, 5), [_m(cash, debit=D("1000")), _m(rev, credit=D("1000"))])
    await _post(db, date(2026, 6, 6), [_m(rent, debit=D("300")), _m(cash, credit=D("300"))])
    await _close_all_2026_months(db)
    return await period_close.close_year(db, year=2026, actor_user_id="admin")


async def _set_december(db, status):
    dec = (await db.execute(
        select(GLPeriod).where(GLPeriod.year == 2026, GLPeriod.period_no == 12))).scalar_one()
    dec.status = status
    await db.flush()


@pytest.mark.asyncio
async def test_a_year_close_entry_cannot_be_reversed(db):
    """NEX-49: reversing a closing entry does not reopen the year — booked today
    it lands in a later year and leaves the old one closed, and either way the
    reversal shows up in the income statement as revenue and expenses. Refused."""
    from fastapi import HTTPException
    from app.models import GLJournalChainHead, GLJournalEntry

    close = await _closed_2026(db)
    # December reopened, so nothing but the rule itself stands in the way.
    await _set_december(db, PeriodStatus.OPEN)
    head = (await db.execute(select(GLJournalChainHead).where(GLJournalChainHead.id == 1))).scalar_one()
    before = (head.row_count, head.latest_entry_hash)

    with pytest.raises(HTTPException) as ei:
        await gl.reverse_entry(db, original_entry_id=close.id, actor_user_id="admin",
                               entry_date=date(2026, 12, 31))
    assert ei.value.status_code == 409
    detail = ei.value.detail
    assert close.entry_no in detail and "2026" in detail
    assert "cannot be reopened by reversing its closing entry" in detail
    assert "correcting manual entry in the current period" in detail

    assert (head.row_count, head.latest_entry_hash) == before
    reversals = (await db.execute(
        select(GLJournalEntry).where(GLJournalEntry.reverses_entry_id.is_not(None)))).scalars().all()
    assert reversals == []
    assert await period_close._year_already_closed(db, 2026) is True


@pytest.mark.asyncio
async def test_reversed_year_close_does_not_count_and_the_year_can_be_closed_again(db):
    """A closing entry can no longer be reversed, but a ledger may already hold
    such a reversal (posted before that rule). It cannot be undone — a reversal
    is never reversed — so that YEAR_CLOSE must stop counting as "already
    closed", or the year could never be closed again."""
    from fastapi import HTTPException
    from app.models import GLJournalLine

    first = await _closed_2026(db)
    assert await period_close._year_already_closed(db, 2026) is True

    # The pre-existing reversal, written the way reverse_entry used to write it.
    await _set_december(db, PeriodStatus.OPEN)
    lines = (await db.execute(
        select(GLJournalLine).where(GLJournalLine.entry_id == first.id)
        .order_by(GLJournalLine.line_no))).scalars().all()
    await gl.post_entry(
        db, entry_date=date(2026, 12, 31), memo=f"Reversal of {first.entry_no}",
        source_type=gl.SOURCE_REVERSAL, source_id=first.id, reverses_entry_id=first.id,
        actor_user_id="admin",
        lines=[_m(l.account_id, debit=l.base_credit, credit=l.base_debit) for l in lines])
    await _set_december(db, PeriodStatus.CLOSED)

    assert await period_close._year_already_closed(db, 2026) is False
    pv = await period_close.year_close_preview(db, year=2026)
    assert pv["already_closed"] is False and pv["net_income"] == D("700.00")

    second = await period_close.close_year(db, year=2026, actor_user_id="admin")
    assert second.id != first.id and second.source_type == "YEAR_CLOSE"

    # Closed once, not twice: P&L back to zero, retained earnings holds 700.
    tb = await compute_trial_balance(db, as_of=date(2026, 12, 31))
    accts = {a["system_key"]: a for a in tb["accounts"] if a["system_key"]}
    assert accts["SALES_REVENUE"]["net_base"] == D("0.00")
    assert accts["RENT_EXPENSE"]["net_base"] == D("0.00")
    assert accts["RETAINED_EARNINGS"]["base_credit"] - accts["RETAINED_EARNINGS"]["base_debit"] == D("700.00")
    assert tb["balanced"] is True

    # And the new close counts: a third attempt is refused like any second one.
    assert await period_close._year_already_closed(db, 2026) is True
    with pytest.raises(HTTPException) as ei:
        await period_close.close_year(db, year=2026, actor_user_id="admin")
    assert ei.value.status_code == 409


@pytest.mark.asyncio
async def test_close_year_blocked_when_month_open(db):
    await _seed(db, months=(6,))
    cash = await _acct(db, "CASH")
    rev = await _acct(db, "SALES_REVENUE")
    await _post(db, date(2026, 6, 5), [_m(cash, debit=D("1000")), _m(rev, credit=D("1000"))])
    with pytest.raises(Exception) as ei:
        await period_close.close_year(db, year=2026, actor_user_id="admin")
    assert "open" in str(ei.value).lower()


# ── Task 5: P&L excludes YEAR_CLOSE ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_closed_year_pnl_still_shows_revenue(db):
    from app.core import statements
    await _seed(db, months=(6,))
    cash = await _acct(db, "CASH")
    rev = await _acct(db, "SALES_REVENUE")
    rent = await _acct(db, "RENT_EXPENSE")
    await _post(db, date(2026, 6, 5), [_m(cash, debit=D("1000")), _m(rev, credit=D("1000"))])
    await _post(db, date(2026, 6, 7), [_m(rent, debit=D("100")), _m(cash, credit=D("100"))])
    await _close_all_2026_months(db)
    await period_close.close_year(db, year=2026, actor_user_id="admin")

    pnl = await statements.income_statement(db, start=date(2026, 1, 1), end=date(2026, 12, 31))
    assert pnl["revenue"] == D("1000.00")
    assert pnl["operating_expenses"] == D("100.00")
    assert pnl["net_profit"] == D("900.00")


# ── Task 6: API symbols ───────────────────────────────────────────────────────

def test_period_close_api_symbols_exist():
    from app.api import accounting as acc
    assert hasattr(acc, "close_readiness_endpoint")
    assert hasattr(acc, "year_close_preview_endpoint")
    assert hasattr(acc, "close_year_endpoint")
