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
    # Standard Lebanese liquidity band: CASH/PETTY → 5300xx (BANK → 5122xx).
    assert 530000 <= int(acct.code) <= 530999


@pytest.mark.asyncio
async def test_adopt_seeded_accounts_idempotent(db):
    await seed_chart_of_accounts(db)
    created = await bank.adopt_seeded_accounts(db)
    assert created == 5  # CASH, CASH_LBP, BANK, CASH_PETTY, CREDIT_CARD_CLEARING
    again = await bank.adopt_seeded_accounts(db)
    assert again == 0
    rows = (await db.execute(select(BankAccount))).scalars().all()
    assert {r.currency for r in rows} == {"USD", "LBP"}


@pytest.mark.asyncio
async def test_adopt_wraps_petty_and_card_clearing(db):
    from app.models import GLAccount
    await seed_chart_of_accounts(db)
    await bank.adopt_seeded_accounts(db)
    wrapped = {
        (await db.execute(select(GLAccount).where(GLAccount.id == b.gl_account_id))).scalar_one().system_key
        for b in (await db.execute(select(BankAccount))).scalars().all()
    }
    assert "CASH_PETTY" in wrapped
    assert "CREDIT_CARD_CLEARING" in wrapped


import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client(db):
    from app.main import app
    from app.deps import get_db, get_current_user
    from app.models import User, Role, Settings
    admin = User(id="u-admin", email="a@x.com", name="A", password_hash="x", role=Role.ADMIN, is_active=True)
    db.add(admin)
    db.add(Settings(id="singleton"))
    await seed_chart_of_accounts(db)
    await db.flush()

    async def _get_db():
        yield db

    async def _get_user():
        return admin

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = _get_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_api_adopt_create_and_cash_position(client):
    assert (await client.post("/api/accounting/bank/adopt-seeded")).status_code == 200
    r = await client.post("/api/accounting/bank/accounts", json={
        "name": "Vault", "account_type": "CASH", "currency": "USD"})
    assert r.status_code == 200, r.text
    cp = (await client.get("/api/accounting/bank/cash-position")).json()
    assert any(a["name"] == "Vault" for a in cp["accounts"])
