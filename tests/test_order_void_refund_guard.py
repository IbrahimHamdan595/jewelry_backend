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
