from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core import gl, bank
from app.core.coa_seed import seed_chart_of_accounts
from app.models import GLPeriod, PeriodStatus, BankAccount, BankAccountType

D = Decimal


async def _setup(db):
    await seed_chart_of_accounts(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    await bank.adopt_seeded_accounts(db)
    await db.flush()
    usd = (await db.execute(select(BankAccount).where(
        BankAccount.currency == "USD", BankAccount.account_type == BankAccountType.CASH))).scalars().first()
    lbp = (await db.execute(select(BankAccount).where(BankAccount.currency == "LBP"))).scalars().first()
    return usd, lbp


def test_usd_base():
    assert bank.usd_base(D("100"), "USD", D("89500")) == D("100.00")
    assert bank.usd_base(D("89500"), "LBP", D("89500")) == D("1.00")


@pytest.mark.asyncio
async def test_transfer_same_currency_balances(db):
    usd_cash, lbp = await _setup(db)
    dest = await bank.create_bank_account(db, name="Vault", account_type=BankAccountType.CASH,
                                          currency="USD", bank_name=None, account_number=None, actor_user_id="u1")
    entry = await bank.post_transfer(db, from_account=usd_cash, to_account=dest, amount=D("500"),
                                     dest_amount=None, memo="move", entry_date=date(2026, 6, 5),
                                     actor_user_id="u1", lbp_rate=D("89500"))
    assert entry.source_type == "TRANSFER"
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    assert tb["balanced"] is True


@pytest.mark.asyncio
async def test_transfer_cross_currency_posts_fx(db):
    usd_cash, lbp = await _setup(db)
    entry = await bank.post_transfer(db, from_account=usd_cash, to_account=lbp, amount=D("100"),
                                     dest_amount=D("9000000"), memo="fx", entry_date=date(2026, 6, 5),
                                     actor_user_id="u1", lbp_rate=D("89500"))
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    assert tb["balanced"] is True
    accts = {a["system_key"]: a for a in tb["accounts"] if a["system_key"]}
    assert ("FX_LOSS" in accts) or ("FX_GAIN" in accts)  # residual posted to the split FX accounts


@pytest.mark.asyncio
async def test_transfer_same_account_rejected(db):
    usd_cash, lbp = await _setup(db)
    with pytest.raises(Exception):
        await bank.post_transfer(db, from_account=usd_cash, to_account=usd_cash, amount=D("1"),
                                 dest_amount=None, memo="x", entry_date=date(2026, 6, 5),
                                 actor_user_id="u1", lbp_rate=D("89500"))
