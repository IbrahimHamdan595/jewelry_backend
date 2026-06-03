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
