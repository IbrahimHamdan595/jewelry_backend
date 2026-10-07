"""NEX-49 — one lock order for the two chain heads: the inventory-ledger head
first, then the GL head.

The sale / void / line-refund / buyback paths write their inventory-ledger
event before they post to the GL, so they hold the ledger head when they ask
for the GL head. A posting made on its own (manual entry, whole-order refund,
AR/AP, year close) used to take the GL head first and the ledger head last, for
its GL_ENTRY_POSTED event: two such requests could wait on each other forever
(AB-BA) and Postgres would abort one. SQLite has no row locks, so these tests
pin the ORDER in which the locks are requested; the deadlock itself is proved on
Postgres.
"""
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql

from app.core import gl
from app.core.coa_seed import seed_chart_of_accounts
from app.models import (
    CoinType, GLAccount, GLJournalEntry, GLPeriod, GoldRateHistory, Karat, MarginMode,
    PeriodStatus, Role, Settings, User,
)

D = Decimal
LEDGER_HEAD = "inventory_ledger_chain_head"
GL_HEAD = "gl_journal_chain_head"


def _row_locks(db) -> list[str]:
    """Tables the session locks FOR UPDATE (as Postgres would see it), in order."""
    seen: list[str] = []

    @event.listens_for(db.sync_session, "do_orm_execute")
    def _spy(state):
        if not state.is_select or state.is_relationship_load or state.is_column_load:
            return
        sql = str(state.statement.compile(dialect=postgresql.dialect()))
        entity = state.statement.column_descriptions[0].get("entity")
        if entity is not None and sql.rstrip().endswith("FOR UPDATE"):
            seen.append(entity.__tablename__)

    return seen


def _assert_ledger_head_before_gl_head(locks: list[str]):
    assert LEDGER_HEAD in locks and GL_HEAD in locks, locks
    assert locks.index(LEDGER_HEAD) < locks.index(GL_HEAD), locks


@pytest.mark.asyncio
async def test_post_entry_locks_the_ledger_head_before_the_gl_head(db):
    await seed_chart_of_accounts(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    await db.flush()
    acct = {a.system_key: a.id for a in (await db.execute(select(GLAccount))).scalars()}
    locks = _row_locks(db)

    await gl.post_entry(
        db, entry_date=date(2026, 6, 3), memo="on its own", source_type=gl.SOURCE_MANUAL,
        source_id=None, actor_user_id="u1",
        lines=[
            gl.GLLine(account_id=acct["CASH"], denomination="", base_debit=D("100"), money_debit=D("100")),
            gl.GLLine(account_id=acct["SALES_REVENUE"], denomination="", base_credit=D("100"), money_credit=D("100")),
        ],
    )
    # Ledger head, GL head, the day's entry_no counter; then ledger.record locks
    # the ledger head again (already held: a no-op) for the GL_ENTRY_POSTED event.
    assert locks == [LEDGER_HEAD, GL_HEAD, "gl_entry_sequence", LEDGER_HEAD]


@pytest.mark.asyncio
async def test_a_refused_posting_takes_no_lock_at_all(db):
    """Validation still comes first: an unbalanced entry must not queue behind
    (or hold up) every other writer just to be turned away."""
    from fastapi import HTTPException

    await seed_chart_of_accounts(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    await db.flush()
    acct = {a.system_key: a.id for a in (await db.execute(select(GLAccount))).scalars()}
    locks = _row_locks(db)

    with pytest.raises(HTTPException):
        await gl.post_entry(
            db, entry_date=date(2026, 6, 3), memo="unbalanced", source_type=gl.SOURCE_MANUAL,
            source_id=None, actor_user_id="u1",
            lines=[
                gl.GLLine(account_id=acct["CASH"], denomination="", base_debit=D("100"), money_debit=D("100")),
                gl.GLLine(account_id=acct["SALES_REVENUE"], denomination="", base_credit=D("90"), money_credit=D("90")),
            ],
        )
    assert locks == []


# ── Every request that posts to the GL, through the real handlers ─────────────

@pytest_asyncio.fixture
async def client(db):
    from app.main import app
    from app.deps import get_db, get_current_user
    admin = User(id="u-admin", email="a@x.com", name="A", password_hash="x", role=Role.ADMIN, is_active=True)
    db.add(admin)
    db.add(Settings(id="singleton", accounting_auto_post_enabled=True))
    await seed_chart_of_accounts(db)
    today = date.today()
    db.add(GLPeriod(year=today.year, period_no=today.month, status=PeriodStatus.OPEN))
    db.add(CoinType(code="C1", name_en="Coin", karat=Karat.K21, weight_grams=D("10"),
                    margin_mode=MarginMode.USD, margin_value=D("5"), on_hand_qty=50))
    db.add(GoldRateHistory(rate_24k=D("60"), source="test"))
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


async def _sale(client, db):
    coin = (await db.execute(select(CoinType))).scalar_one()
    r = await client.post("/api/orders", json={
        "payment_method": "CASH", "items": [{"item_kind": "COIN", "coin_type_id": coin.id, "quantity": 1}]})
    assert r.status_code == 201, r.text
    return r.json()


async def _manual_entry(client, db):
    acct = {a.system_key: a.id for a in (await db.execute(select(GLAccount))).scalars()}
    r = await client.post("/api/accounting/journal-entries", json={
        "entry_date": date.today().isoformat(), "memo": "manual", "source_type": "MANUAL",
        "lines": [
            {"account_id": acct["CASH"], "base_debit": "100", "money_debit": "100"},
            {"account_id": acct["SALES_REVENUE"], "base_credit": "100", "money_credit": "100"},
        ]})
    assert r.status_code == 200, r.text
    return r.json()


# name → (setup returning what the request needs, the request itself)
async def _flow_sale(client, db, _):
    return await client.post("/api/orders", json={
        "payment_method": "CASH",
        "items": [{"item_kind": "COIN", "coin_type_id": (await db.execute(select(CoinType))).scalar_one().id,
                   "quantity": 1}]})


async def _flow_void(client, db, order):
    return await client.post(f"/api/orders/{order['id']}/void", json={"reason": "x"})


async def _flow_refund(client, db, order):
    return await client.post(f"/api/orders/{order['id']}/refund")


async def _flow_item_refund(client, db, order):
    return await client.post(f"/api/orders/{order['id']}/items/{order['items'][0]['id']}/refund", json={})


async def _flow_manual(client, db, _):
    acct = {a.system_key: a.id for a in (await db.execute(select(GLAccount))).scalars()}
    return await client.post("/api/accounting/journal-entries", json={
        "entry_date": date.today().isoformat(), "memo": "manual",
        "lines": [
            {"account_id": acct["CASH"], "base_debit": "100", "money_debit": "100"},
            {"account_id": acct["SALES_REVENUE"], "base_credit": "100", "money_credit": "100"},
        ]})


async def _flow_reverse(client, db, entry):
    return await client.post(f"/api/accounting/journal-entries/{entry['id']}/reverse")


async def _flow_ar_invoice(client, db, customer):
    return await client.post("/api/accounting/ar/invoices", json={
        "customer_id": customer["id"], "invoice_date": date.today().isoformat(), "vat_percent": "11",
        "lines": [{"description": "svc", "quantity": 1, "unit_price": "200"}]})


async def _customer(client, db):
    r = await client.post("/api/accounting/ar/customers", json={"name": "Acme", "credit_limit": "1000"})
    assert r.status_code == 200, r.text
    return r.json()


async def _nothing(client, db):
    return None


FLOWS = {
    # record their inventory event first, then post
    "sale": (_nothing, _flow_sale),
    "void": (_sale, _flow_void),
    "line refund": (_sale, _flow_item_refund),
    # post on their own
    "whole-order refund": (_sale, _flow_refund),
    "manual entry": (_nothing, _flow_manual),
    "reversal": (_manual_entry, _flow_reverse),
    "AR invoice": (_customer, _flow_ar_invoice),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", FLOWS)
async def test_every_gl_posting_request_locks_the_ledger_head_first(client, db, name):
    setup, request = FLOWS[name]
    given = await setup(client, db)
    before = len((await db.execute(select(GLJournalEntry))).scalars().all())
    locks = _row_locks(db)

    r = await request(client, db, given)
    assert r.status_code in (200, 201), r.text
    # It really did post (otherwise there would be nothing to order) ...
    assert len((await db.execute(select(GLJournalEntry))).scalars().all()) == before + 1
    # ... and asked for the two heads in the one agreed order.
    _assert_ledger_head_before_gl_head(locks)
