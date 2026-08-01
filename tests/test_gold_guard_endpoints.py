"""Integration: the stale-rate guard on the sale endpoint.

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
from app.models import GoldRateHistory, InventoryLedger, Karat, Order, Product, Settings, User
from app.schemas.order import CheckoutRequest, OrderItemIn

STALE_FETCHED_AT = datetime.now(timezone.utc) - timedelta(hours=3)


# Async fixtures need @pytest_asyncio.fixture (strict mode) — see conftest.py.
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


@pytest_asyncio.fixture
async def product(db):
    """Minimal sellable product so a checkout can actually complete.

    Without this every test aborts in _checkout_product_line and never reaches
    the audit-row wiring this task exists to add.
    """
    p = Product(
        id="p-1",
        code="TEST-001",
        name_en="Test Ring",
        category="RING",
        karat=Karat.K21,
        weight_grams=Decimal("5"),
        margin_percent=Decimal("10"),
        making_charge=Decimal("0"),
        on_hand_qty=5,
    )
    db.add(p)
    await db.commit()
    return p


def _checkout(ack=None):
    return CheckoutRequest(
        items=[OrderItemIn(item_kind="PRODUCT", product_id="p-1", quantity=1)],
        payment_method="CASH",
        stale_rate_ack=ack,
    )


@pytest.mark.asyncio
async def test_stale_sale_without_ack_is_rejected_and_writes_nothing(
    db, stale_rate, cashier, settings_row, product
):
    with pytest.raises(HTTPException) as exc:
        await create_order(_checkout(), db=db, user=cashier)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "STALE_RATE_ACK_REQUIRED"

    # The guard must fire BEFORE any row is built — order OR ledger.
    assert (await db.execute(select(func.count()).select_from(Order))).scalar() == 0
    assert (
        await db.execute(select(func.count()).select_from(InventoryLedger))
    ).scalar() == 0


@pytest.mark.asyncio
async def test_stale_sale_with_mismatched_ack_is_rejected(
    db, stale_rate, cashier, settings_row, product
):
    wrong = StaleRateAck(rate_fetched_at=STALE_FETCHED_AT + timedelta(minutes=7))
    with pytest.raises(HTTPException) as exc:
        await create_order(_checkout(wrong), db=db, user=cashier)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "STALE_RATE_ACK_MISMATCH"
    assert (await db.execute(select(func.count()).select_from(Order))).scalar() == 0
    assert (
        await db.execute(select(func.count()).select_from(InventoryLedger))
    ).scalar() == 0


@pytest.mark.asyncio
async def test_stale_sale_with_matching_ack_completes_and_records_one_row(
    db, stale_rate, cashier, settings_row, product
):
    """The whole point of the task: the sale lands, and its justification row
    lands with it, in the same chain."""
    ack = StaleRateAck(rate_fetched_at=STALE_FETCHED_AT)
    out = await create_order(_checkout(ack), db=db, user=cashier)

    assert out.id
    rows = (
        await db.execute(
            select(InventoryLedger).where(
                InventoryLedger.event_type == "SALE_ON_STALE_RATE_ACK"
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].ref_type == "order"
    assert rows[0].ref_id == out.id
    assert rows[0].actor_user_id == cashier.id
    assert rows[0].payload["context"] == "ORDER"

    # Chained to the SALE row, not merely present. `prev_hash and entry_hash !=
    # prev_hash` would be vacuous: prev_hash is non-nullable and seeded to
    # GENESIS, and entry_hash is a sha256 over content including prev_hash, so
    # they can never be equal. Asserting the actual link is what catches the ack
    # row being appended somewhere it can be detached from the sale.
    sale_row = (
        await db.execute(
            select(InventoryLedger).where(InventoryLedger.event_type == "SALE_PRODUCT")
        )
    ).scalar_one()
    assert rows[0].prev_hash == sale_row.entry_hash


@pytest.mark.asyncio
async def test_unnecessary_ack_on_a_fresh_rate_writes_no_row(
    db, cashier, settings_row, product
):
    """A client that always attaches an ack must not pollute the audit trail —
    otherwise 'cashier knowingly traded on an old price' becomes indistinguishable
    from routine noise."""
    fresh = GoldRateHistory(
        id="fresh-1",
        rate_24k=Decimal("84.31"),
        source="live",
        fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(fresh)
    await db.commit()

    ack = StaleRateAck(rate_fetched_at=datetime.now(timezone.utc))
    out = await create_order(_checkout(ack), db=db, user=cashier)

    assert out.id
    acks = (
        await db.execute(
            select(func.count())
            .select_from(InventoryLedger)
            .where(InventoryLedger.event_type == "SALE_ON_STALE_RATE_ACK")
        )
    ).scalar()
    assert acks == 0
