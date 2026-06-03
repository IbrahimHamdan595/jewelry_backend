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


import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client(db):
    """App client whose get_db yields the test session, with auth stubbed to an
    ADMIN user. Mirrors how other API tests inject the in-memory session."""
    from app.main import app
    from app.deps import get_db, get_current_user
    from app.models import User, Role

    admin = User(id="u-admin", email="a@x.com", name="Admin",
                 password_hash="x", role=Role.ADMIN, is_active=True)
    db.add(admin)
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
async def test_seed_then_open_period_then_post_and_trial_balance(client):
    # Seed CoA
    r = await client.post("/api/accounting/seed-coa")
    assert r.status_code == 200

    # Open June 2026
    r = await client.post("/api/accounting/periods", json={"year": 2026, "period_no": 6})
    assert r.status_code == 200

    # Look up CASH + SALES_REVENUE account ids
    accts = (await client.get("/api/accounting/accounts")).json()["items"]
    by_key = {a["system_key"]: a["id"] for a in accts}

    # Post a balanced manual entry
    payload = {
        "entry_date": "2026-06-03", "memo": "cash sale", "source_type": "MANUAL",
        "lines": [
            {"account_id": by_key["CASH"], "base_debit": "100", "money_debit": "100"},
            {"account_id": by_key["SALES_REVENUE"], "base_credit": "100", "money_credit": "100"},
        ],
    }
    r = await client.post("/api/accounting/journal-entries", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["entry_no"] == "JE-20260603-001"

    # Trial balance balances
    tb = (await client.get("/api/accounting/trial-balance?as_of=2026-06-30")).json()
    assert tb["balanced"] is True
    assert tb["total_base_debit"] == "100.00"

    # Chain verify intact
    v = (await client.get("/api/accounting/ledger/verify")).json()
    assert v["status"] == "intact"


@pytest.mark.asyncio
async def test_unbalanced_entry_rejected_422(client):
    await client.post("/api/accounting/seed-coa")
    await client.post("/api/accounting/periods", json={"year": 2026, "period_no": 6})
    accts = (await client.get("/api/accounting/accounts")).json()["items"]
    by_key = {a["system_key"]: a["id"] for a in accts}
    payload = {
        "entry_date": "2026-06-03", "memo": "bad", "source_type": "MANUAL",
        "lines": [
            {"account_id": by_key["CASH"], "base_debit": "100"},
            {"account_id": by_key["SALES_REVENUE"], "base_credit": "90"},
        ],
    }
    r = await client.post("/api/accounting/journal-entries", json=payload)
    assert r.status_code == 422
