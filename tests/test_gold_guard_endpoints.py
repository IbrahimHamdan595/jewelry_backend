"""Integration: the stale-rate guard on the sale and buyback endpoints.

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

from app.api.buybacks import create_buyback
from app.api.orders import create_order
from app.models import (
    GoldRateHistory,
    InventoryLedger,
    Karat,
    Order,
    Product,
    Settings,
    User,
    WalkinBuyback,
)
from app.schemas.buyback import BuybackCreate
from app.schemas.gold_rate import StaleRateAck
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
    cashier_id = cashier.id  # read before the rollback below expires the object

    ack = StaleRateAck(rate_fetched_at=STALE_FETCHED_AT)
    out = await create_order(_checkout(ack), db=db, user=cashier)

    assert out.id

    # Assert against COMMITTED state only. create_order commits internally, so
    # anything still pending here was written outside its transaction. Without
    # this rollback the assertions below would also pass for an ack row appended
    # *after* db.commit() — it would sit uncommitted in this very session and
    # still be visible to these queries.
    await db.rollback()

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
    assert rows[0].actor_user_id == cashier_id
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


# ── Buybacks ─────────────────────────────────────────────────────────────────


def _buyback(ack=None):
    """PURE_GOLD with a manual price — the cheapest complete path.

    `manual_price` makes _resolve_margin return MANUAL, so no Settings margin
    defaults are needed, and post_buyback no-ops because auto-posting is off on a
    default Settings row. The handler creates its own GoldLot, so no lot fixture.

    NOT using USED_PRODUCT despite it being simpler: `_create_used_product_buyback`
    passes an undefined `cfg` to `gl_postings.post_buyback` (its signature never
    receives one), so that kind raises NameError before it ever commits. That is a
    pre-existing bug, deliberately left unfixed here so it is not buried inside a
    feature commit; `test_used_product_buyback_records_the_ack` below pins it.
    """
    return BuybackCreate(
        seller_name="Walk-in Seller",
        seller_phone="+96170000000",
        kind="PURE_GOLD",
        karat="K21",
        weight_grams=Decimal("10"),
        manual_price=Decimal("500"),
        stale_rate_ack=ack,
    )


@pytest.mark.asyncio
async def test_stale_buyback_without_ack_is_rejected_and_writes_nothing(
    db, stale_rate, cashier, settings_row
):
    with pytest.raises(HTTPException) as exc:
        await create_buyback(_buyback(), db=db, user=cashier)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "STALE_RATE_ACK_REQUIRED"
    assert (
        await db.execute(select(func.count()).select_from(WalkinBuyback))
    ).scalar() == 0
    assert (
        await db.execute(select(func.count()).select_from(InventoryLedger))
    ).scalar() == 0


@pytest.mark.asyncio
async def test_stale_buyback_with_ack_completes_and_records_one_chained_row(
    db, stale_rate, cashier, settings_row
):
    """The whole design in one test: the buyback lands, and its justification
    row lands with it, in the same chain."""
    cashier_id = cashier.id  # read before the rollback below expires the object

    ack = StaleRateAck(rate_fetched_at=STALE_FETCHED_AT)
    receipt = await create_buyback(_buyback(ack), db=db, user=cashier)

    assert receipt.id

    # Assert against COMMITTED state only. The handler commits internally, so
    # anything still pending here was written outside its transaction. Without
    # this rollback the assertions below would also pass for an ack row appended
    # *after* db.commit() — it would sit unflushed/uncommitted in this very
    # session and still be visible to these queries.
    await db.rollback()

    assert (
        await db.execute(select(func.count()).select_from(WalkinBuyback))
    ).scalar() == 1

    rows = (
        await db.execute(
            select(InventoryLedger)
            .where(InventoryLedger.event_type == "SALE_ON_STALE_RATE_ACK")
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].ref_type == "walkin_buyback"
    assert rows[0].ref_id == receipt.id
    assert rows[0].actor_user_id == cashier_id
    assert rows[0].payload["context"] == "BUYBACK"
    assert rows[0].payload["rate_24k"] == "84.31"
    # Bounded on both sides: `>= 179` alone would also accept a unit bug that
    # reported 10800 seconds.
    assert 179 <= rows[0].payload["age_minutes"] <= 181

    # Chained to the buyback's own ledger row, not merely present. Asserting
    # `prev_hash` is truthy would be vacuous — it is non-nullable and seeded to
    # GENESIS.
    buyback_row = (
        await db.execute(
            select(InventoryLedger)
            .where(InventoryLedger.event_type == "BUYBACK_PURE_GOLD")
        )
    ).scalar_one()
    assert rows[0].prev_hash == buyback_row.entry_hash


@pytest.mark.asyncio
async def test_unnecessary_ack_on_a_fresh_buyback_writes_no_row(
    db, cashier, settings_row
):
    """A client that always attaches an ack must not pollute the audit trail."""
    fresh = GoldRateHistory(
        id="fresh-2",
        rate_24k=Decimal("84.31"),
        source="live",
        fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(fresh)
    await db.commit()

    ack = StaleRateAck(rate_fetched_at=datetime.now(timezone.utc))
    receipt = await create_buyback(_buyback(ack), db=db, user=cashier)
    assert receipt.id

    acks = (
        await db.execute(
            select(func.count())
            .select_from(InventoryLedger)
            .where(InventoryLedger.event_type == "SALE_ON_STALE_RATE_ACK")
        )
    ).scalar()
    assert acks == 0


@pytest.mark.asyncio
async def test_ack_row_rolls_back_with_the_buyback(
    db, stale_rate, cashier, settings_row, monkeypatch
):
    """The reason the ack is recorded INSIDE the handler, not after it.

    Inject a failure between the ack row and the commit. Neither the buyback nor
    its justification row may survive — a committed sale with no justification is
    exactly the state the hash chain exists to make impossible.
    """
    from app.api import buybacks as buybacks_module

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated failure before commit")

    monkeypatch.setattr(buybacks_module.gl_postings, "post_buyback", boom)

    ack = StaleRateAck(rate_fetched_at=STALE_FETCHED_AT)
    with pytest.raises(RuntimeError):
        await create_buyback(_buyback(ack), db=db, user=cashier)

    await db.rollback()

    assert (
        await db.execute(select(func.count()).select_from(WalkinBuyback))
    ).scalar() == 0
    acks = (
        await db.execute(
            select(func.count())
            .select_from(InventoryLedger)
            .where(InventoryLedger.event_type == "SALE_ON_STALE_RATE_ACK")
        )
    ).scalar()
    assert acks == 0


@pytest.mark.xfail(
    raises=NameError,
    strict=True,
    reason=(
        "pre-existing bug: _create_used_product_buyback passes an undefined cfg to "
        "gl_postings.post_buyback. When that is fixed this test XPASSes and strict=True "
        "turns it into a failure — remove the marker and confirm the ack row lands."
    ),
)
@pytest.mark.asyncio
async def test_used_product_buyback_records_the_ack(
    db, stale_rate, cashier, settings_row
):
    """USED_PRODUCT is wired identically to the other kinds but unreachable today."""
    body = BuybackCreate(
        seller_name="Walk-in Seller",
        seller_phone="+96170000000",
        kind="USED_PRODUCT",
        karat="K21",
        weight_grams=Decimal("10"),
        manual_price=Decimal("500"),
        stale_rate_ack=StaleRateAck(rate_fetched_at=STALE_FETCHED_AT),
    )
    receipt = await create_buyback(body, db=db, user=cashier)

    assert receipt.id

    # Committed state only — see the sibling PURE_GOLD test for why.
    await db.rollback()

    assert (
        await db.execute(select(func.count()).select_from(WalkinBuyback))
    ).scalar() == 1

    rows = (
        await db.execute(
            select(InventoryLedger)
            .where(InventoryLedger.event_type == "SALE_ON_STALE_RATE_ACK")
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].ref_type == "walkin_buyback"
    assert rows[0].ref_id == receipt.id
    assert rows[0].payload["context"] == "BUYBACK"

    # Chained, not merely present — the property the feature actually guarantees.
    # Written now so whoever fixes `cfg` inherits the full assertion rather than
    # a weaker one; it does not execute until this test stops xfailing.
    buyback_row = (
        await db.execute(
            select(InventoryLedger)
            .where(InventoryLedger.event_type == "BUYBACK_USED_PRODUCT")
        )
    ).scalar_one()
    assert rows[0].prev_hash == buyback_row.entry_hash
