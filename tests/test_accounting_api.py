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


# ── NEX-49: a manual entry may not claim a source type the system posts under ─

async def _manual_payload(client, **over):
    """Seed + open June 2026 and return a balanced manual-entry payload."""
    await client.post("/api/accounting/seed-coa")
    await client.post("/api/accounting/periods", json={"year": 2026, "period_no": 6})
    accts = (await client.get("/api/accounting/accounts")).json()["items"]
    by_key = {a["system_key"]: a["id"] for a in accts}
    return {
        "entry_date": "2026-06-03", "memo": "manual",
        "lines": [
            {"account_id": by_key["CASH"], "base_debit": "100", "money_debit": "100"},
            {"account_id": by_key["SALES_REVENUE"], "base_credit": "100", "money_credit": "100"},
        ],
        **over,
    }


async def _entry_total(client) -> int:
    return (await client.get("/api/accounting/journal-entries")).json()["total"]


def test_reserved_source_types_cover_everything_the_system_posts_under():
    """Enumerated from the code: every SOURCE_* constant (and YEAR_CLOSE) of every
    app.core module that posts to the GL, except MANUAL. A new posting module or
    source type fails here until it is reserved too."""
    import importlib
    import inspect
    import pkgutil

    import app.core as core_pkg
    from app.api.accounting import RESERVED_SOURCE_TYPES
    from app.core import gl

    system = set()
    for info in pkgutil.iter_modules(core_pkg.__path__):
        mod = importlib.import_module(f"app.core.{info.name}")
        if "post_entry(" not in inspect.getsource(mod):
            continue
        system |= {v for k, v in vars(mod).items()
                   if isinstance(v, str) and (k.startswith("SOURCE_") or k == "YEAR_CLOSE")}
    system.discard(gl.SOURCE_MANUAL)

    assert len(system) == 15, sorted(system)
    assert RESERVED_SOURCE_TYPES == system


@pytest.mark.asyncio
async def test_manual_entry_rejects_every_reserved_source_type(client):
    from app.api.accounting import RESERVED_SOURCE_TYPES

    payload = await _manual_payload(client)
    for st in sorted(RESERVED_SOURCE_TYPES):
        r = await client.post("/api/accounting/journal-entries", json={**payload, "source_type": st})
        assert r.status_code == 422, (st, r.text)
        assert "reserved" in r.json()["detail"], st
    # Spelling tricks do not get round it.
    r = await client.post("/api/accounting/journal-entries", json={**payload, "source_type": " order "})
    assert r.status_code == 422, r.text
    assert await _entry_total(client) == 0


@pytest.mark.asyncio
async def test_manual_entry_still_accepts_what_the_frontend_sends(client):
    payload = await _manual_payload(client)
    # The journal page posts source_type "MANUAL" and no source_id ...
    r = await client.post("/api/accounting/journal-entries", json={**payload, "source_type": "MANUAL"})
    assert r.status_code == 200, r.text
    assert r.json()["source_type"] == "MANUAL" and r.json()["source_id"] is None
    # ... and leaving source_type out means MANUAL too.
    r = await client.post("/api/accounting/journal-entries", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["source_type"] == "MANUAL"
    # A free-text reference on a manual entry is still fine, and may repeat.
    for _ in range(2):
        r = await client.post("/api/accounting/journal-entries",
                              json={**payload, "source_type": "MANUAL", "source_id": "ref-1"})
        assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_manual_order_entry_cannot_preempt_the_real_sale_posting(client, db):
    """A manual (ORDER, <order id>) entry used to be accepted; find_live_entry
    then took it for the sale's entry and post_sale silently posted nothing."""
    from decimal import Decimal as D
    from app.core import gl_postings
    from app.models import (
        GLJournalEntry, Karat, Order, OrderItem, OrderItemKind, PaymentMethod, Settings,
    )
    from tests.conftest import BOOK_DATETIME

    payload = await _manual_payload(client)
    order = Order(
        order_number="ORD-1", cashier_id="u-admin", payment_method=PaymentMethod.CASH,
        subtotal=D("100"), vat_percent=D("11"), vat_amount=D("11"),
        discount_percent=D("0"), discount_amount=D("0"),
        total_usd=D("111"), total_lbp=D("9934500"), lbp_exchange_rate=D("89500"),
        created_at=BOOK_DATETIME,
    )
    order.items = [OrderItem(
        item_kind=OrderItemKind.COIN, product_code="C1", product_name="Coin", karat=Karat.K21,
        weight_grams=D("10.000"), gold_rate_at_sale=D("60.00"), margin_percent=D("0"),
        making_charge=D("0"), final_price=D("100"), quantity=1,
    )]
    db.add(order)
    await db.flush()

    r = await client.post("/api/accounting/journal-entries",
                          json={**payload, "source_type": "ORDER", "source_id": order.id})
    assert r.status_code == 422, r.text

    cfg = Settings(id="singleton", accounting_auto_post_enabled=True, vat_percent=D("11"),
                   lbp_exchange_rate=D("89500"))
    sale = await gl_postings.post_sale(db, order, cfg, "u-admin")
    assert sale is not None and sale.source_type == "ORDER" and sale.source_id == order.id
    from sqlalchemy import select
    assert len((await db.execute(select(GLJournalEntry))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_manual_year_close_entry_is_rejected_and_does_not_close_the_year(client, db):
    from app.core import period_close

    payload = await _manual_payload(client)
    r = await client.post("/api/accounting/journal-entries",
                          json={**payload, "source_type": "YEAR_CLOSE"})
    assert r.status_code == 422, r.text
    assert await period_close._year_already_closed(db, 2026) is False


@pytest.mark.asyncio
async def test_manual_reversal_without_reverses_entry_id_is_rejected(client):
    """A manual entry labelled REVERSAL carries no reverses_entry_id, so neither
    the guard nor the index would know the entry it claims to reverse."""
    payload = await _manual_payload(client)
    orig = (await client.post("/api/accounting/journal-entries", json=payload)).json()

    r = await client.post("/api/accounting/journal-entries",
                          json={**payload, "source_type": "REVERSAL", "source_id": orig["id"]})
    assert r.status_code == 422, r.text
    assert await _entry_total(client) == 1



# ── NEX-49: a year-close entry cannot be reversed ─────────────────────────────

@pytest.mark.asyncio
async def test_reverse_endpoint_refuses_a_year_close_entry(client):
    payload = await _manual_payload(client)
    assert (await client.post("/api/accounting/journal-entries", json=payload)).status_code == 200
    period = (await client.get("/api/accounting/periods")).json()["items"][0]
    assert (await client.post(f"/api/accounting/periods/{period['id']}/close")).status_code == 200
    r = await client.post("/api/accounting/periods/close-year", json={"year": 2026})
    assert r.status_code == 200, r.text
    close_id, close_no = r.json()["entry_id"], r.json()["entry_no"]

    # The endpoint books a reversal today: make sure today's period is open, so
    # the rule is the only thing that can refuse it (whatever the date is).
    from datetime import date
    today = date.today()
    r = await client.post("/api/accounting/periods", json={"year": today.year, "period_no": today.month})
    if r.status_code == 409:
        periods = (await client.get("/api/accounting/periods")).json()["items"]
        pid = next(p["id"] for p in periods if (p["year"], p["period_no"]) == (today.year, today.month))
        assert (await client.post(f"/api/accounting/periods/{pid}/reopen")).status_code == 200

    r = await client.post(f"/api/accounting/journal-entries/{close_id}/reverse")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert close_no in detail and "2026" in detail
    assert "cannot be reopened by reversing its closing entry" in detail
    assert "correcting manual entry in the current period" in detail

    # Nothing posted, chain untouched, and the year is still closed.
    assert await _entry_total(client) == 2
    v = (await client.get("/api/accounting/ledger/verify")).json()
    assert v["status"] == "intact" and v["head_matches"] is True and v["head_row_count"] == 2
    pv = (await client.get("/api/accounting/periods/year-close-preview?year=2026")).json()
    assert pv["already_closed"] is True
