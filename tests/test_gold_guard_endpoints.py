"""Integration: the stale-rate guard on the two money-moving endpoints.

The point of these tests is the SIDE EFFECT, not the status code. A guard that
returns 409 after already writing an Order row would be worse than no guard, so
each rejection case asserts the table is still empty.

Endpoint functions are called directly (not over HTTP) — the same style as
tests/test_gold_market.py, which calls `history(...)` directly. This keeps the
in-memory SQLite fixture and avoids standing up the full app, which pulls in
rate limiting and CORS we do not need here.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api.orders import create_order
from app.core.gold_guard import StaleRateAck
from app.models import GoldRateHistory, InventoryLedger, Order, Settings, User
from app.schemas.order import CheckoutRequest, OrderItemIn

STALE_FETCHED_AT = datetime.now(timezone.utc) - timedelta(hours=3)


# NOTE: async fixtures MUST use @pytest_asyncio.fixture, not @pytest.fixture.
# pytest-asyncio runs in strict mode here (1.x), where a plain @pytest.fixture
# async function is never awaited — the test receives a coroutine object and
# fails with a confusing AttributeError. tests/conftest.py uses the same
# decorator for its `db` fixture.
@pytest_asyncio.fixture
async def stale_rate(db):
    """A rate old enough that get_current_gold_rate flags market_closed."""
    row = GoldRateHistory(
        id="stale-1",
        rate_24k=Decimal("84.31"),
        source="live",
        fetched_at=STALE_FETCHED_AT.replace(tzinfo=None),
    )
    db.add(row)
    await db.commit()
    return row


@pytest_asyncio.fixture
async def cashier(db):
    user = User(
        id="u-cashier",
        email="cashier@example.com",
        name="Test Cashier",
        password_hash="x",
        role="CASHIER",
        is_active=True,
    )
    db.add(user)
    await db.commit()
    return user


@pytest_asyncio.fixture
async def settings_row(db):
    cfg = Settings(id="singleton")
    db.add(cfg)
    await db.commit()
    return cfg


def _checkout(ack=None):
    return CheckoutRequest(
        items=[OrderItemIn(item_kind="PRODUCT", product_id="p-1", quantity=1)],
        payment_method="CASH",
        stale_rate_ack=ack,
    )


@pytest.mark.asyncio
async def test_stale_sale_without_ack_is_rejected_and_writes_nothing(
    db, stale_rate, cashier, settings_row
):
    with pytest.raises(HTTPException) as exc:
        await create_order(_checkout(), db=db, user=cashier)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "STALE_RATE_ACK_REQUIRED"

    # The guard must fire BEFORE any row is built.
    assert (await db.execute(select(func.count()).select_from(Order))).scalar() == 0


@pytest.mark.asyncio
async def test_stale_sale_with_mismatched_ack_is_rejected(
    db, stale_rate, cashier, settings_row
):
    wrong = StaleRateAck(rate_fetched_at=STALE_FETCHED_AT + timedelta(minutes=7))
    with pytest.raises(HTTPException) as exc:
        await create_order(_checkout(wrong), db=db, user=cashier)

    assert exc.value.detail["code"] == "STALE_RATE_ACK_MISMATCH"
    assert (await db.execute(select(func.count()).select_from(Order))).scalar() == 0


@pytest.mark.asyncio
async def test_ack_is_not_recorded_when_the_rate_is_fresh(db, cashier, settings_row):
    """No ack required, no audit noise. Normal trading adds zero extra rows."""
    fresh = GoldRateHistory(
        id="fresh-1",
        rate_24k=Decimal("84.31"),
        source="live",
        fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(fresh)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await create_order(_checkout(), db=db, user=cashier)
    # Fails on the missing product, NOT on the guard — proves the guard let it through.
    # (_checkout_product_line raises 400 "Invalid product ..." for a missing/
    # inactive product, not 404 — confirmed by reading app/api/orders.py.)
    assert exc.value.status_code == 400

    acks = (
        await db.execute(
            select(func.count())
            .select_from(InventoryLedger)
            .where(InventoryLedger.event_type == "SALE_ON_STALE_RATE_ACK")
        )
    ).scalar()
    assert acks == 0
