from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core import gl
from app.models import (
    GLAccount, GLPeriod, PeriodStatus, AccountType, Denomination, NormalBalance,
)

D = Decimal


async def _setup(db):
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    cash = GLAccount(code="1000", name="Cash", type=AccountType.ASSET,
                     denomination=Denomination.MONEY, normal_balance=NormalBalance.DEBIT,
                     currency="USD", system_key="CASH")
    rev = GLAccount(code="4000", name="Sales", type=AccountType.INCOME,
                    denomination=Denomination.MONEY, normal_balance=NormalBalance.CREDIT,
                    currency="USD", system_key="SALES_REVENUE")
    inv = GLAccount(code="1200", name="Metal Inventory", type=AccountType.ASSET,
                    denomination=Denomination.DUAL, normal_balance=NormalBalance.DEBIT,
                    currency="USD", system_key="METAL_INVENTORY")
    cogs = GLAccount(code="5000", name="Metal COGS", type=AccountType.EXPENSE,
                     denomination=Denomination.DUAL, normal_balance=NormalBalance.DEBIT,
                     currency="USD", system_key="METAL_COGS")
    db.add_all([cash, rev, inv, cogs])
    await db.flush()
    return cash, rev, inv, cogs


@pytest.mark.asyncio
async def test_trial_balance_identity_and_per_karat(db):
    cash, rev, inv, cogs = await _setup(db)
    await gl.post_entry(
        db, entry_date=date(2026, 6, 3), memo="sale", source_type=gl.SOURCE_MANUAL,
        source_id=None, actor_user_id="u1",
        lines=[
            gl.GLLine(account_id=cash.id, denomination="MONEY", base_debit=D("100"), money_debit=D("100")),
            gl.GLLine(account_id=rev.id, denomination="MONEY", base_credit=D("100"), money_credit=D("100")),
            gl.GLLine(account_id=cogs.id, denomination="DUAL", base_debit=D("60"),
                      metal_debit_grams=D("10.000"), karat="K21"),
            gl.GLLine(account_id=inv.id, denomination="DUAL", base_credit=D("60"),
                      metal_credit_grams=D("10.000"), karat="K21"),
        ],
    )
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    assert tb["total_base_debit"] == tb["total_base_credit"] == D("160.00")
    assert tb["balanced"] is True
    # Metal position per karat nets to zero across the ledger.
    assert tb["metal_by_karat"]["K21"]["debit_grams"] == tb["metal_by_karat"]["K21"]["credit_grams"]

    inv_row = next(a for a in tb["accounts"] if a["system_key"] == "METAL_INVENTORY")
    assert inv_row["metal_by_karat"]["K21"]["net_grams"] == D("-10.000")  # credit-out reduces inventory


@pytest.mark.asyncio
async def test_trial_balance_as_of_excludes_future(db):
    cash, rev, inv, cogs = await _setup(db)
    db.add(GLPeriod(year=2026, period_no=7, status=PeriodStatus.OPEN))
    await db.flush()
    await gl.post_entry(
        db, entry_date=date(2026, 7, 5), memo="july sale", source_type=gl.SOURCE_MANUAL,
        source_id=None, actor_user_id="u1",
        lines=[
            gl.GLLine(account_id=cash.id, denomination="MONEY", base_debit=D("50"), money_debit=D("50")),
            gl.GLLine(account_id=rev.id, denomination="MONEY", base_credit=D("50"), money_credit=D("50")),
        ],
    )
    tb_june = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    assert tb_june["total_base_debit"] == D("0.00")  # July excluded
    tb_july = await gl.compute_trial_balance(db, as_of=date(2026, 7, 31))
    assert tb_july["total_base_debit"] == D("50.00")


# ── SQL aggregation vs the Python replay oracle (NEX-53) ──────────────────────
import json

from sqlalchemy import event

from app.core.audit_chain import GENESIS_HASH
from app.models import GLJournalEntry, GLJournalLine
from tests.conftest import BOOK_DATETIME

_RAW_SEQ = [0]


async def _setup_wide(db):
    """Accounts across both dimensions: two currencies, DUAL metal accounts."""
    for m in (5, 6, 7):
        db.add(GLPeriod(year=2026, period_no=m, status=PeriodStatus.OPEN))

    def acct(code, name, type_, denom, normal, key, currency="USD"):
        return GLAccount(code=code, name=name, type=type_, denomination=denom,
                         normal_balance=normal, currency=currency, system_key=key)

    A, L, EQ, INC, EXP = (AccountType.ASSET, AccountType.LIABILITY, AccountType.EQUITY,
                          AccountType.INCOME, AccountType.EXPENSE)
    M, DU = Denomination.MONEY, Denomination.DUAL
    DR, CR = NormalBalance.DEBIT, NormalBalance.CREDIT
    accts = {
        "cash": acct("1000", "Cash", A, M, DR, "CASH"),
        "cash_lbp": acct("1010", "Cash LBP", A, M, DR, "CASH_LBP", currency="LBP"),
        "inv": acct("1200", "Metal Inventory", A, DU, DR, "METAL_INVENTORY"),
        "metal_ap": acct("2100", "Metal AP", L, DU, CR, "METAL_AP"),
        "vat": acct("2200", "VAT Payable", L, M, CR, "VAT_PAYABLE"),
        "equity": acct("3000", "Opening Equity", EQ, DU, CR, "OPENING_BALANCE_EQUITY"),
        "rev": acct("4000", "Sales", INC, M, CR, "SALES_REVENUE"),
        "cogs": acct("5000", "Metal COGS", EXP, DU, DR, "METAL_COGS"),
    }
    db.add_all(accts.values())
    await db.flush()
    return accts


async def _post(db, entry_date, lines, memo="t"):
    return await gl.post_entry(db, entry_date=entry_date, memo=memo, source_type=gl.SOURCE_MANUAL,
                               source_id=None, actor_user_id="u1", lines=lines)


def _money(acct, *, debit=D("0"), credit=D("0")):
    return gl.GLLine(account_id=acct.id, denomination="MONEY", base_debit=debit, base_credit=credit,
                     money_debit=debit, money_credit=credit)


def _lbp(acct, *, base_debit=D("0"), base_credit=D("0"), money_debit=D("0"), money_credit=D("0")):
    return gl.GLLine(account_id=acct.id, denomination="MONEY", base_debit=base_debit,
                     base_credit=base_credit, money_debit=money_debit, money_credit=money_credit,
                     currency="LBP", fx_rate=D("89500"))


def _metal(acct, karat, *, base_debit=D("0"), base_credit=D("0"), dr_grams=D("0"), cr_grams=D("0")):
    return gl.GLLine(account_id=acct.id, denomination="DUAL", base_debit=base_debit,
                     base_credit=base_credit, money_debit=base_debit, money_credit=base_credit,
                     metal_debit_grams=dr_grams, metal_credit_grams=cr_grams, karat=karat)


async def _raw_line(db, entry_date, acct, **cols):
    """Insert a line straight into the tables, bypassing post_entry's validation,
    to reach shapes the posting engine refuses (metal without a karat)."""
    period = (await db.execute(select(GLPeriod).where(
        GLPeriod.year == entry_date.year, GLPeriod.period_no == entry_date.month))).scalar_one()
    _RAW_SEQ[0] += 1
    entry = GLJournalEntry(entry_no=f"RAW-{_RAW_SEQ[0]}", entry_date=entry_date, period_id=period.id,
                           memo="raw", source_type="TEST", actor_user_id="u1",
                           occurred_at=BOOK_DATETIME, prev_hash=GENESIS_HASH,
                           entry_hash=f"raw-hash-{_RAW_SEQ[0]}")
    db.add(entry)
    await db.flush()
    db.add(GLJournalLine(entry_id=entry.id, account_id=acct.id, **cols))
    await db.flush()


async def _post_wide_ledger(db, a):
    """Multi-account, multi-currency, multi-karat ledger with a reversed entry,
    activity after the June cut-off, and float-unfriendly amounts."""
    # May opening: cash + K24 bars contributed as capital.
    await _post(db, date(2026, 5, 20), [
        _money(a["cash"], debit=D("1000.10")),
        _metal(a["inv"], "K24", base_debit=D("7001.07"), dr_grams=D("100.001")),
        _metal(a["equity"], "K24", base_credit=D("8001.17"), cr_grams=D("100.001")),
    ])
    # June USD sale with VAT + K21 cost of sale.
    await _post(db, date(2026, 6, 3), [
        _money(a["cash"], debit=D("100.10")),
        _money(a["rev"], credit=D("90.18")),
        _money(a["vat"], credit=D("9.92")),
        _metal(a["cogs"], "K21", base_debit=D("60.07"), dr_grams=D("10.005")),
        _metal(a["inv"], "K21", base_credit=D("60.07"), cr_grams=D("10.005")),
    ])
    # June LBP sale: revenue now carries a second currency.
    await _post(db, date(2026, 6, 4), [
        _lbp(a["cash_lbp"], base_debit=D("11.17"), money_debit=D("1000000.00")),
        _lbp(a["rev"], base_credit=D("11.17"), money_credit=D("1000000.00")),
    ])
    # June K18 purchase on metal credit (grams owed) + K21 top-up.
    await _post(db, date(2026, 6, 5), [
        _metal(a["inv"], "K18", base_debit=D("150.33"), dr_grams=D("3.333")),
        _metal(a["metal_ap"], "K18", base_credit=D("150.33"), cr_grams=D("3.333")),
        _metal(a["inv"], "K21", base_debit=D("0.10"), dr_grams=D("0.017")),
        _metal(a["metal_ap"], "K21", base_credit=D("0.10"), cr_grams=D("0.017")),
    ])
    # Thirty 0.10 sales: binary floats cannot sum these exactly.
    for _ in range(30):
        await _post(db, date(2026, 6, 6), [_money(a["cash"], debit=D("0.10")),
                                           _money(a["rev"], credit=D("0.10"))])
    # A mistaken K21 sale on the 10th, reversed on the 12th.
    wrong = await _post(db, date(2026, 6, 10), [
        _money(a["cash"], debit=D("555.55")),
        _money(a["rev"], credit=D("555.55")),
        _metal(a["cogs"], "K21", base_debit=D("333.33"), dr_grams=D("5.555")),
        _metal(a["inv"], "K21", base_credit=D("333.33"), cr_grams=D("5.555")),
    ])
    await gl.reverse_entry(db, original_entry_id=wrong.id, actor_user_id="u1",
                           entry_date=date(2026, 6, 12))
    # July activity: beyond the June cut-off.
    await _post(db, date(2026, 7, 5), [
        _money(a["cash"], debit=D("77.77")),
        _money(a["rev"], credit=D("77.77")),
        _metal(a["cogs"], "K22", base_debit=D("40.00"), dr_grams=D("1.250")),
        _metal(a["inv"], "K22", base_credit=D("40.00"), cr_grams=D("1.250")),
    ])


def _canon(tb: dict) -> str:
    """Order-insensitive, type-strict rendering: every amount must be a Decimal
    and renders with its exact scale, so 3.00 != 3.0 and a float can't pass."""
    def enc(v):
        if isinstance(v, Decimal):
            return f"Decimal:{v}"
        if isinstance(v, date):
            return v.isoformat()
        raise TypeError(f"unexpected {type(v).__name__} in trial balance: {v!r}")
    return json.dumps(tb, default=enc, sort_keys=True)


_CUTOFFS = [date(2026, 4, 30), date(2026, 5, 31), date(2026, 6, 4), date(2026, 6, 11),
            date(2026, 6, 30), date(2026, 7, 31)]


@pytest.mark.asyncio
async def test_trial_balance_sql_matches_python_replay(db):
    a = await _setup_wide(db)
    await _post_wide_ledger(db, a)

    for as_of in _CUTOFFS:
        sql = await gl.compute_trial_balance(db, as_of=as_of)
        replay = await gl.compute_trial_balance_replay(db, as_of=as_of)
        assert _canon(sql) == _canon(replay), f"as_of={as_of}"
        assert [x["code"] for x in sql["accounts"]] == [x["code"] for x in replay["accounts"]]
        assert sql["balanced"] is True and sql["metal_balanced"] is True

    june = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    by_key = {x["system_key"]: x for x in june["accounts"]}
    # Reversal nets the mistaken sale out; the 30 × 0.10 sales sum exactly.
    assert by_key["CASH"]["net_base"] == D("1103.20")
    assert str(by_key["CASH"]["net_base"]) == "1103.20"
    assert by_key["SALES_REVENUE"]["money_by_currency"] == {
        "USD": {"debit": D("555.55"), "credit": D("648.73")},
        "LBP": {"debit": D("0.00"), "credit": D("1000000.00")},
    }
    assert by_key["METAL_INVENTORY"]["metal_by_karat"]["K21"]["net_grams"] == D("-9.988")
    assert str(by_key["METAL_INVENTORY"]["metal_by_karat"]["K18"]["net_grams"]) == "3.333"
    assert set(june["metal_by_karat"]) == {"K18", "K21", "K24"}          # K22 is July-only
    # Mid-June, before the reversal lands, the mistaken sale is still in.
    mid = await gl.compute_trial_balance(db, as_of=date(2026, 6, 11))
    assert next(x for x in mid["accounts"] if x["system_key"] == "CASH")["net_base"] == D("1658.75")


@pytest.mark.asyncio
async def test_trial_balance_sql_matches_replay_on_unvalidated_lines(db):
    """Lines the posting engine would refuse still replay identically: metal
    with no karat buckets under "?", a karat-tagged money line adds no karat
    bucket, and an unbalanced ledger reports balanced=False on both paths."""
    a = await _setup_wide(db)
    await _raw_line(db, date(2026, 6, 2), a["inv"], base_debit=D("5.00"), money_debit=D("5.00"),
                    metal_debit_grams=D("1.500"), karat=None)
    await _raw_line(db, date(2026, 6, 2), a["inv"], base_debit=D("1.00"), money_debit=D("1.00"),
                    metal_debit_grams=D("0.250"), karat="")
    await _raw_line(db, date(2026, 6, 2), a["cash"], base_debit=D("9.99"), money_debit=D("9.99"),
                    karat="K21")

    sql = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    replay = await gl.compute_trial_balance_replay(db, as_of=date(2026, 6, 30))
    assert _canon(sql) == _canon(replay)
    by_key = {x["system_key"]: x for x in sql["accounts"]}
    assert by_key["METAL_INVENTORY"]["metal_by_karat"] == {
        "?": {"debit_grams": D("1.750"), "credit_grams": D("0.000"), "net_grams": D("1.750")}}
    assert by_key["CASH"]["metal_by_karat"] == {}
    assert sql["balanced"] is False and sql["metal_balanced"] is False


@pytest.mark.asyncio
async def test_trial_balance_aggregates_in_sql(db):
    """One GROUP BY round-trip, however many lines the ledger holds — the
    replay it replaces shipped every journal line to Python."""
    a = await _setup_wide(db)
    await _post_wide_ledger(db, a)

    statements = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.bind.sync_engine, "before_cursor_execute", _capture)
    try:
        await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", _capture)

    assert len(statements) == 1
    assert "GROUP BY" in statements[0] and "sum(" in statements[0].lower()
