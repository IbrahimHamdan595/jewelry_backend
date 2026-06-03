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
