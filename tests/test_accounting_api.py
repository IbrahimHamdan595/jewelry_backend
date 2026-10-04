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


# ── NEX-49: a journal entry is reversed at most once ──────────────────────────

async def _post_dual_sale_today(client):
    """Seed, open the CURRENT month (the reverse endpoint books date.today()),
    and post a manual sale touching both the money and the metal dimension."""
    from datetime import date
    today = date.today()
    await client.post("/api/accounting/seed-coa")
    r = await client.post("/api/accounting/periods", json={"year": today.year, "period_no": today.month})
    assert r.status_code == 200, r.text
    accts = (await client.get("/api/accounting/accounts")).json()["items"]
    by_key = {a["system_key"]: a["id"] for a in accts}
    payload = {
        "entry_date": today.isoformat(), "memo": "cash sale", "source_type": "MANUAL",
        "lines": [
            {"account_id": by_key["CASH"], "base_debit": "100", "money_debit": "100"},
            {"account_id": by_key["SALES_REVENUE"], "base_credit": "100", "money_credit": "100"},
            {"account_id": by_key["METAL_COGS"], "base_debit": "60",
             "metal_debit_grams": "10.000", "karat": "K21"},
            {"account_id": by_key["METAL_INVENTORY"], "base_credit": "60",
             "metal_credit_grams": "10.000", "karat": "K21"},
        ],
    }
    r = await client.post("/api/accounting/journal-entries", json=payload)
    assert r.status_code == 200, r.text
    return r.json(), today


@pytest.mark.asyncio
async def test_reverse_endpoint_second_reversal_is_409_not_a_new_entry(client):
    orig, today = await _post_dual_sale_today(client)

    r1 = await client.post(f"/api/accounting/journal-entries/{orig['id']}/reverse")
    assert r1.status_code == 200, r1.text
    assert r1.json()["reverses_entry_id"] == orig["id"]

    # The double-click.
    r2 = await client.post(f"/api/accounting/journal-entries/{orig['id']}/reverse")
    assert r2.status_code == 409, r2.text
    assert orig["entry_no"] in r2.json()["detail"]

    # Nothing was posted and the chain has no gap.
    assert (await client.get("/api/accounting/journal-entries")).json()["total"] == 2
    v = (await client.get("/api/accounting/ledger/verify")).json()
    assert v["status"] == "intact" and v["head_matches"] is True
    assert v["head_row_count"] == 2 and v["head_latest_hash"] == r1.json()["entry_hash"]

    # One reversal nets the original to zero — in USD and in grams per karat.
    tb = (await client.get(f"/api/accounting/trial-balance?as_of={today.isoformat()}")).json()
    assert tb["balanced"] is True and tb["metal_balanced"] is True
    assert len(tb["accounts"]) == 4
    for a in tb["accounts"]:
        assert a["net_base"] == "0.00", a["code"]
        for k, m in a["metal_by_karat"].items():
            assert m["net_grams"] == "0.000", (a["code"], k)


@pytest.mark.asyncio
async def test_reverse_endpoint_refuses_to_reverse_a_reversal(client):
    orig, _ = await _post_dual_sale_today(client)
    rev = (await client.post(f"/api/accounting/journal-entries/{orig['id']}/reverse")).json()

    r = await client.post(f"/api/accounting/journal-entries/{rev['id']}/reverse")
    assert r.status_code == 409, r.text

    assert (await client.get("/api/accounting/journal-entries")).json()["total"] == 2
    v = (await client.get("/api/accounting/ledger/verify")).json()
    assert v["status"] == "intact" and v["head_matches"] is True
    assert v["head_row_count"] == 2 and v["head_latest_hash"] == rev["entry_hash"]
