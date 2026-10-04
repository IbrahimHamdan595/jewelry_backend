"""NEX-52 — the accounting auto-post flag over the settings API.

Until this ticket the flag lived only on the Settings model: the GL engine
could not be switched on without a manual UPDATE. These tests pin the API
contract the merged frontend (NEX-58) already codes against:

  • GET  /api/settings returns `accounting_auto_post_enabled` as a real boolean
    (the UI treats anything else as "not reported" and disables the switch);
  • PATCH /api/settings {accounting_auto_post_enabled: bool} flips it — ADMIN
    only — and the flip lands on the audit ledger like every other knob;
  • a PATCH that does not carry the field (another settings tab saving) can
    never move it.
"""
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.coa_seed import seed_chart_of_accounts
from app.core.ledger import EVENT_SETTINGS_CHANGED
from app.models import (
    CoinType, GLAccount, GLJournalEntry, GLJournalLine, GoldRateHistory, InventoryLedger,
    Karat, MarginMode, Role, Settings, User,
)

D = Decimal
FLAG = "accounting_auto_post_enabled"


@pytest_asyncio.fixture
async def api(db):
    """App client on the in-memory session with one user per role. `as_role`
    swaps who the request is authenticated as; require_admin still runs for
    real, so a 403 here is the production 403."""
    from app.main import app
    from app.deps import get_db, get_current_user

    users = {
        role: User(id=f"u-{role.value.lower()}", email=f"{role.value.lower()}@x.com",
                   name=role.value, password_hash="x", role=role, is_active=True)
        for role in Role
    }
    db.add_all(users.values())
    db.add(Settings(id="singleton"))
    await seed_chart_of_accounts(db)  # like a shop that has opened the accounting section
    await db.flush()
    current = {"user": users[Role.ADMIN]}

    async def _get_db():
        yield db

    async def _get_user():
        return current["user"]

    def as_role(role: Role) -> None:
        current["user"] = users[role]

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = _get_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield SimpleNamespace(client=c, as_role=as_role)
    app.dependency_overrides.clear()


async def _stored_flag(db) -> bool:
    s = (await db.execute(select(Settings).where(Settings.id == "singleton"))).scalar_one()
    await db.refresh(s)
    return s.accounting_auto_post_enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("role", list(Role))
async def test_get_settings_reports_the_flag_as_a_boolean(api, role):
    """Every authenticated role reads it: the accounting hub (ACCOUNTANT) shows
    whether the books are live, not only the admin settings screen."""
    api.as_role(role)
    r = await api.client.get("/api/settings")
    assert r.status_code == 200, r.text
    assert r.json()[FLAG] is False  # the model default — and a bool, not null/absent


@pytest.mark.asyncio
async def test_admin_can_turn_the_flag_on_and_off(api, db):
    r = await api.client.patch("/api/settings", json={FLAG: True})
    assert r.status_code == 200, r.text
    assert r.json()[FLAG] is True
    assert await _stored_flag(db) is True
    assert (await api.client.get("/api/settings")).json()[FLAG] is True

    r = await api.client.patch("/api/settings", json={FLAG: False})
    assert r.status_code == 200, r.text
    assert r.json()[FLAG] is False
    assert await _stored_flag(db) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [r for r in Role if r is not Role.ADMIN])
async def test_non_admin_cannot_change_the_flag(api, db, role):
    """CASHIER, ACCOUNTANT and MANAGER are all refused — in both directions."""
    api.as_role(role)
    r = await api.client.patch("/api/settings", json={FLAG: True})
    assert r.status_code == 403, r.text
    assert await _stored_flag(db) is False

    # …and cannot switch it off once an admin has switched it on.
    api.as_role(Role.ADMIN)
    assert (await api.client.patch("/api/settings", json={FLAG: True})).status_code == 200
    api.as_role(role)
    r = await api.client.patch("/api/settings", json={FLAG: False})
    assert r.status_code == 403, r.text
    assert await _stored_flag(db) is True


@pytest.mark.asyncio
async def test_flag_cannot_be_turned_on_before_the_chart_of_accounts_is_seeded(api, db):
    """With the flag on, every sale resolves the system accounts and 422s if one
    is missing — one click would stop the till. Refuse the click instead, and
    say what to do; the settings screen shows this message in its prompt."""
    await db.execute(delete(GLAccount))
    await db.flush()

    r = await api.client.patch("/api/settings", json={FLAG: True, "receipt_footer": "x"})

    assert r.status_code == 409, r.text
    assert "chart of accounts" in r.json()["detail"].lower()
    assert await _stored_flag(db) is False
    assert (await api.client.get("/api/settings")).json()["receipt_footer"] is None  # all or nothing

    # Seeding is all it takes — and switching OFF is never blocked.
    await seed_chart_of_accounts(db)
    await db.flush()
    assert (await api.client.patch("/api/settings", json={FLAG: True})).status_code == 200
    await db.execute(delete(GLAccount))
    await db.flush()
    assert (await api.client.patch("/api/settings", json={FLAG: False})).status_code == 200
    assert await _stored_flag(db) is False


@pytest.mark.asyncio
async def test_flipping_the_flag_is_recorded_on_the_audit_ledger(api, db):
    await api.client.patch("/api/settings", json={FLAG: True})
    rows = (
        await db.execute(
            select(InventoryLedger).where(InventoryLedger.event_type == EVENT_SETTINGS_CHANGED)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].actor_user_id == "u-admin"
    assert rows[0].payload["diff"] == {FLAG: {"from": False, "to": True}}


@pytest.mark.asyncio
async def test_a_payload_without_the_flag_leaves_it_unchanged(api, db):
    """The frontend's general save sends every OTHER settings field. Absent
    must mean "leave unchanged" — never "reset to the default"."""
    await api.client.patch("/api/settings", json={FLAG: True})

    r = await api.client.patch("/api/settings", json={
        "store_name": "Fawaz El Namel", "vat_percent": "11", "receipt_footer": "Thank you",
    })
    assert r.status_code == 200, r.text
    assert r.json()[FLAG] is True
    assert r.json()["receipt_footer"] == "Thank you"
    assert await _stored_flag(db) is True


@pytest.mark.asyncio
async def test_an_explicit_null_leaves_the_flag_unchanged(api, db):
    """A stale form that serialises the field as null must not reset the switch
    (nor 500 on the NOT NULL column) — and the rest of that save still applies."""
    await api.client.patch("/api/settings", json={FLAG: True})

    r = await api.client.patch("/api/settings", json={FLAG: None, "receipt_footer": "Merci"})
    assert r.status_code == 200, r.text
    assert r.json()[FLAG] is True
    assert r.json()["receipt_footer"] == "Merci"
    assert await _stored_flag(db) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["true", "off", 1, 0, "yes"])
async def test_only_a_real_boolean_moves_the_flag(api, db, bad):
    """No truthy-string coercion on a master switch: "off" must not parse to
    False, 1 must not parse to True."""
    r = await api.client.patch("/api/settings", json={FLAG: bad})
    assert r.status_code == 422, r.text
    assert await _stored_flag(db) is False


@pytest.mark.asyncio
async def test_sale_after_admin_enables_the_flag_posts_a_balanced_entry(api, db):
    """Acceptance: flag switched on THROUGH THE API, on books as empty as
    production's (chart seeded, zero periods, zero entries) → the next sale
    posts a balanced entry and the trial balance holds in money and metal."""
    coin = CoinType(code="C-NEX52", name_en="Coin", karat=Karat.K21, weight_grams=D("10"),
                    margin_mode=MarginMode.USD, margin_value=D("5"), on_hand_qty=5)
    db.add(coin)
    db.add(GoldRateHistory(rate_24k=D("60"), source="test"))
    await db.flush()

    assert (await api.client.patch("/api/settings", json={FLAG: True})).status_code == 200

    api.as_role(Role.CASHIER)  # the till, not the admin, rings up the sale
    r = await api.client.post("/api/orders", json={
        "payment_method": "CASH",
        "items": [{"item_kind": "COIN", "coin_type_id": coin.id, "quantity": 1}],
    })
    assert r.status_code == 201, r.text

    entry = (
        await db.execute(select(GLJournalEntry).where(GLJournalEntry.source_type == "ORDER"))
    ).scalar_one()
    assert entry.source_id == r.json()["id"]
    lines = (
        await db.execute(select(GLJournalLine).where(GLJournalLine.entry_id == entry.id))
    ).scalars().all()
    assert sum(l.base_debit for l in lines) == sum(l.base_credit for l in lines) > 0
    assert sum(l.metal_debit_grams for l in lines) == sum(l.metal_credit_grams for l in lines) == D("10.000")

    api.as_role(Role.ADMIN)
    tb = (await api.client.get("/api/accounting/trial-balance", params={"as_of": "2999-12-31"})).json()
    assert tb["balanced"] is True and tb["metal_balanced"] is True
    assert D(tb["total_base_debit"]) == D(tb["total_base_credit"]) > 0
