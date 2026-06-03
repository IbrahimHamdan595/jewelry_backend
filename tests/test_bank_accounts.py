import pytest
from sqlalchemy import select

from app.models import (
    BankAccount, BankStatementLine, Reconciliation,
    BankAccountType, StatementLineStatus, ReconciliationStatus,
)


def test_bank_enums():
    assert {t.value for t in BankAccountType} == {"CASH", "BANK", "PETTY_CASH"}
    assert {s.value for s in StatementLineStatus} == {"UNMATCHED", "MATCHED"}
    assert {r.value for r in ReconciliationStatus} == {"OPEN", "COMPLETED"}


@pytest.mark.asyncio
async def test_bank_models_create(db):
    from app.models import GLAccount, AccountType, Denomination, NormalBalance
    acct = GLAccount(code="1100", name="Main Bank", type=AccountType.ASSET,
                     denomination=Denomination.MONEY, normal_balance=NormalBalance.DEBIT, currency="USD")
    db.add(acct)
    await db.flush()
    ba = BankAccount(name="Main Bank", gl_account_id=acct.id, account_type=BankAccountType.BANK, currency="USD")
    db.add(ba)
    await db.flush()
    assert ba.id and ba.is_active is True


from app.core import bank
from app.core.coa_seed import seed_chart_of_accounts
from app.models import GLAccount, Denomination


@pytest.mark.asyncio
async def test_create_bank_account_makes_gl_account(db):
    ba = await bank.create_bank_account(
        db, name="Petty Cash", account_type=BankAccountType.PETTY_CASH,
        currency="USD", bank_name=None, account_number=None, actor_user_id="u1",
    )
    acct = (await db.execute(select(GLAccount).where(GLAccount.id == ba.gl_account_id))).scalar_one()
    assert acct.denomination == Denomination.MONEY
    assert 1100 <= int(acct.code) <= 1999


@pytest.mark.asyncio
async def test_adopt_seeded_accounts_idempotent(db):
    await seed_chart_of_accounts(db)
    created = await bank.adopt_seeded_accounts(db)
    assert created == 3  # CASH, CASH_LBP, BANK
    again = await bank.adopt_seeded_accounts(db)
    assert again == 0
    rows = (await db.execute(select(BankAccount))).scalars().all()
    assert {r.currency for r in rows} == {"USD", "LBP"}
