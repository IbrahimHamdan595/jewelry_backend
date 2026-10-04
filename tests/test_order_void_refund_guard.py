"""NEX-49 — voiding / fully refunding an order: exactly one stock restore and
at most one reversal of the sale entry, however the requests arrive."""
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.coa_seed import seed_chart_of_accounts
from app.models import (
    CoinType, GLJournalEntry, GoldRateHistory, Karat, MarginMode, Order, OrderStatus,
    Role, Settings, User,
)

D = Decimal


@pytest_asyncio.fixture
async def client(db):
    from app.main import app
    from app.deps import get_db, get_current_user
    admin = User(id="u-admin", email="a@x.com", name="A", password_hash="x", role=Role.ADMIN, is_active=True)
    db.add(admin)
    db.add(Settings(id="singleton", accounting_auto_post_enabled=True))
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


async def _sell_one_coin(client, db):
    """A real checkout: 1 of 5 coins sold, sale auto-posted to the GL."""
    coin = CoinType(code="C1", name_en="Coin", karat=Karat.K21, weight_grams=D("10"),
                    margin_mode=MarginMode.USD, margin_value=D("5"), on_hand_qty=5)
    db.add(coin)
    db.add(GoldRateHistory(rate_24k=D("60"), source="test"))
    await db.flush()
    r = await client.post("/api/orders", json={
        "payment_method": "CASH",
        "items": [{"item_kind": "COIN", "coin_type_id": coin.id, "quantity": 1}],
    })
    assert r.status_code == 201, r.text
    sale = (await db.execute(
        select(GLJournalEntry).where(GLJournalEntry.source_type == "ORDER"))).scalar_one()
    assert coin.on_hand_qty == 4
    return r.json()["id"], coin, sale


async def _reversals(db) -> int:
    return (await db.execute(
        select(func.count()).select_from(GLJournalEntry)
        .where(GLJournalEntry.reverses_entry_id.is_not(None)))).scalar_one()


# ── Sale entry already reversed by hand: the void/refund still completes ──────

@pytest.mark.asyncio
async def test_void_completes_when_the_sale_entry_was_already_reversed_by_hand(client, db):
    order_id, coin, sale = await _sell_one_coin(client, db)
    r = await client.post(f"/api/accounting/journal-entries/{sale.id}/reverse")
    assert r.status_code == 200, r.text

    r = await client.post(f"/api/orders/{order_id}/void", json={"reason": "customer changed mind"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "VOIDED"
    assert coin.on_hand_qty == 5                # stock came back
    assert await _reversals(db) == 1            # and the books were not reversed twice
    v = (await client.get("/api/accounting/ledger/verify")).json()
    assert v["status"] == "intact" and v["head_matches"] is True and v["head_row_count"] == 2


@pytest.mark.asyncio
async def test_refund_completes_when_the_sale_entry_was_already_reversed_by_hand(client, db):
    order_id, _, sale = await _sell_one_coin(client, db)
    r = await client.post(f"/api/accounting/journal-entries/{sale.id}/reverse")
    assert r.status_code == 200, r.text

    r = await client.post(f"/api/orders/{order_id}/refund")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "REFUNDED"
    assert await _reversals(db) == 1


# ── One void wins: the order row is locked and its status re-read ─────────────
# The real race needs two connections (proved on Postgres, see NEX-49); SQLite
# has no row locks. These pin the two properties the handlers rely on: the
# order is read FOR UPDATE, and the status that is checked is the locked row's.

def _order_selects(db):
    """Postgres SQL of every primary SELECT of Order the session runs."""
    from sqlalchemy import event
    from sqlalchemy.dialects import postgresql

    seen: list[str] = []

    @event.listens_for(db.sync_session, "do_orm_execute")
    def _spy(state):
        if not state.is_select or state.is_relationship_load or state.is_column_load:
            return
        if state.statement.column_descriptions[0].get("entity") is Order:
            seen.append(str(state.statement.compile(dialect=postgresql.dialect())))

    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("action, body", [("void", {"reason": "x"}), ("refund", None)])
async def test_void_and_refund_lock_the_order_row(client, db, action, body):
    order_id, _, _ = await _sell_one_coin(client, db)
    seen = _order_selects(db)

    r = await client.post(f"/api/orders/{order_id}/{action}", json=body)
    assert r.status_code == 200, r.text
    assert seen and seen[0].rstrip().endswith("FOR UPDATE"), seen[:1]


@pytest.mark.asyncio
@pytest.mark.parametrize("action, body", [("void", {"reason": "x"}), ("refund", None)])
async def test_void_and_refund_check_the_locked_row_not_a_stale_copy(client, db, action, body):
    """While this request waited for the lock, another one voided the order. The
    session may still hold the pre-lock copy (status COMPLETED); the handler must
    judge by the row it just locked, and refuse before restoring any stock."""
    from sqlalchemy import text

    order_id, coin, _ = await _sell_one_coin(client, db)
    order = await db.get(Order, order_id)       # the copy the session already holds
    await db.execute(text("UPDATE orders SET status = 'VOIDED' WHERE id = :id"), {"id": order_id})
    assert order.status == OrderStatus.COMPLETED  # ... is now stale

    r = await client.post(f"/api/orders/{order_id}/{action}", json=body)
    assert r.status_code == 400, r.text
    assert coin.on_hand_qty == 4                # nothing restored
    assert await _reversals(db) == 0


@pytest.mark.asyncio
async def test_second_void_is_refused_with_one_restore_and_one_reversal(client, db):
    order_id, coin, _ = await _sell_one_coin(client, db)

    r1 = await client.post(f"/api/orders/{order_id}/void", json={"reason": "x"})
    assert r1.status_code == 200, r1.text
    r2 = await client.post(f"/api/orders/{order_id}/void", json={"reason": "x"})
    assert r2.status_code == 400 and "already voided" in r2.json()["detail"]
    r3 = await client.post(f"/api/orders/{order_id}/refund")
    assert r3.status_code == 400

    assert coin.on_hand_qty == 5
    assert await _reversals(db) == 1
