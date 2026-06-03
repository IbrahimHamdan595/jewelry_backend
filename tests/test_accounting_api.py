import pytest

from app.models import Role, AccountType, Denomination, NormalBalance, PeriodStatus


def test_role_enum_has_accounting_roles():
    assert Role.ACCOUNTANT.value == "ACCOUNTANT"
    assert Role.MANAGER.value == "MANAGER"
    # Existing roles unchanged
    assert Role.ADMIN.value == "ADMIN"
    assert Role.CASHIER.value == "CASHIER"


def test_gl_enums_present():
    assert {t.value for t in AccountType} == {
        "ASSET", "LIABILITY", "EQUITY", "INCOME", "EXPENSE"
    }
    assert {d.value for d in Denomination} == {"MONEY", "METAL", "DUAL"}
    assert {n.value for n in NormalBalance} == {"DEBIT", "CREDIT"}
    assert {p.value for p in PeriodStatus} == {"OPEN", "CLOSED"}


@pytest.mark.asyncio
async def test_gl_models_create_and_chain_head_seeded(db):
    from sqlalchemy import select
    from app.models import (
        GLAccount, GLJournalChainHead, AccountType, Denomination, NormalBalance,
    )
    from app.core.audit_chain import GENESIS_HASH

    head = (
        await db.execute(select(GLJournalChainHead).where(GLJournalChainHead.id == 1))
    ).scalar_one()
    assert head.latest_entry_hash == GENESIS_HASH
    assert head.row_count == 0

    acct = GLAccount(
        code="1000", name="Cash", type=AccountType.ASSET,
        denomination=Denomination.MONEY, normal_balance=NormalBalance.DEBIT,
        currency="USD", system_key="CASH",
    )
    db.add(acct)
    await db.flush()
    assert acct.id
