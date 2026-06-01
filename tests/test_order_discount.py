"""Phase 2 — order-level discount. Exercises create_order end-to-end against the
in-memory DB (deterministic rate via an active GoldRateOverride). Covers
checkpoints 2.1, 2.4, 2.5 and the receipt discount display (2.3)."""
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.orders import create_order, refund_order_item
from app.core.receipt import build_sale_receipt
from app.models import (
    CoinType,
    GoldRateOverride,
    MarginMode,
    Order,
    Settings,
    User,
    Role,
)
from app.schemas.order import CheckoutRequest, ItemRefundRequest, OrderItemIn


async def _seed(db, *, max_discount="10", coin_qty=1):
    """Coin priced to exactly $500/unit at rate 100 (5g × $100 + $0 markup +
    $0... actually margin USD 0 → metal 500). qty controls subtotal."""
    admin = User(id="admin1", email="a@x.com", name="Admin", password_hash="x", role=Role.ADMIN)
    settings = Settings(
        id="singleton", vat_percent=Decimal("11"), lbp_exchange_rate=Decimal("89500"),
        max_discount_percent=Decimal(max_discount),
    )
    # effective_rate = 100 + 0 = 100; metal = 100 * 5 = 500; margin USD 0 → final 500.
    coin = CoinType(
        id="c1", code="MZB-COIN-22K-0001", name_en="Lira", karat="K22",
        weight_grams=Decimal("5.000"), markup_per_gram=Decimal("0"),
        margin_mode=MarginMode.USD, margin_value=Decimal("0"), on_hand_qty=100,
    )
    override = GoldRateOverride(rate_24k=Decimal("100"), set_by="admin1", is_active=True)
    db.add_all([admin, settings, coin, override])
    await db.commit()
    return admin


@pytest.mark.asyncio
async def test_2_1_exact_pre_discount_vat_math(db):
    # $500 coin × 2 = $1000 subtotal. 11% VAT on the ORIGINAL $1000 = $110.
    # 10% discount = $100 off the grand total: 1000 + 110 − 100 = 1010.
    admin = await _seed(db, max_discount="10")
    payload = CheckoutRequest(
        items=[OrderItemIn(item_kind="COIN", coin_type_id="c1", quantity=2)],
        payment_method="CASH",
        discount_percent=Decimal("10"),
    )
    out = await create_order(payload, db=db, user=admin)
    assert out.subtotal == Decimal("1000.00")
    assert out.vat_amount == Decimal("110.00")        # VAT on pre-discount subtotal
    assert out.discount_percent == Decimal("10")
    assert out.discount_amount == Decimal("100.00")
    assert out.total_usd == Decimal("1010.00")        # 1000 + 110 − 100
    assert out.total_lbp == Decimal("90395000.00")    # 1010 * 89500


@pytest.mark.asyncio
async def test_2_4_discount_above_cap_rejected_422(db):
    admin = await _seed(db, max_discount="10")
    payload = CheckoutRequest(
        items=[OrderItemIn(item_kind="COIN", coin_type_id="c1", quantity=1)],
        payment_method="CASH",
        discount_percent=Decimal("15"),  # > 10 cap
    )
    with pytest.raises(HTTPException) as exc:
        await create_order(payload, db=db, user=admin)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_2_zero_discount_is_unaffected(db):
    admin = await _seed(db, max_discount="0")  # discounts disabled
    payload = CheckoutRequest(
        items=[OrderItemIn(item_kind="COIN", coin_type_id="c1", quantity=2)],
        payment_method="CASH",
    )
    out = await create_order(payload, db=db, user=admin)
    assert out.discount_amount == Decimal("0.00")
    assert out.total_usd == Decimal("1110.00")  # 1000 + 110, no discount


@pytest.mark.asyncio
async def test_2_5_discounted_order_refunds_proportionally(db):
    # $500 × 2 = 1000, 10% discount. Refund 1 of 2 coins → remaining subtotal 500,
    # discount must scale to $50 (10% of 500), VAT 55, total 505.
    admin = await _seed(db, max_discount="10")
    payload = CheckoutRequest(
        items=[OrderItemIn(item_kind="COIN", coin_type_id="c1", quantity=2)],
        payment_method="CASH",
        discount_percent=Decimal("10"),
    )
    out = await create_order(payload, db=db, user=admin)
    item_id = out.items[0].id

    await refund_order_item(out.id, item_id, ItemRefundRequest(quantity=1), db=db, user=admin)
    order = (await db.execute(select(Order).where(Order.id == out.id))).scalar_one()
    assert order.subtotal == Decimal("500.00")
    assert order.vat_amount == Decimal("55.00")
    assert order.discount_amount == Decimal("50.00")   # proportional
    assert order.total_usd == Decimal("505.00")        # 500 + 55 − 50


@pytest.mark.asyncio
async def test_2_5b_refund_cash_is_discount_and_vat_adjusted(db):
    # 2×$500 = 1000 subtotal, 10% discount, 11% VAT → total 1010.
    # Refunding 1 coin returns the DISCOUNTED, VAT-inclusive cash: 500×1.01 = 505,
    # i.e. the order total drops 1010 → 505. The ledger records that exact figure.
    from sqlalchemy import select as _select
    from app.models import InventoryLedger
    from app.core.ledger import EVENT_ORDER_ITEM_REFUND

    admin = await _seed(db, max_discount="10")
    out = await create_order(
        CheckoutRequest(
            items=[OrderItemIn(item_kind="COIN", coin_type_id="c1", quantity=2)],
            payment_method="CASH", discount_percent=Decimal("10"),
        ),
        db=db, user=admin,
    )
    await refund_order_item(out.id, out.items[0].id, ItemRefundRequest(quantity=1), db=db, user=admin)

    ev = (
        await db.execute(
            _select(InventoryLedger).where(InventoryLedger.event_type == EVENT_ORDER_ITEM_REFUND)
        )
    ).scalars().all()
    assert len(ev) == 1
    # pre-discount internal figure vs actual cash returned
    assert ev[0].payload["refund_amount"] == "500.00"
    assert ev[0].payload["cash_refunded_usd"] == "505.00"


@pytest.mark.asyncio
async def test_2_3_receipt_shows_discount(db):
    admin = await _seed(db, max_discount="10")
    payload = CheckoutRequest(
        items=[OrderItemIn(item_kind="COIN", coin_type_id="c1", quantity=2)],
        payment_method="CASH",
        discount_percent=Decimal("10"),
    )
    out = await create_order(payload, db=db, user=admin)

    order = (
        await db.execute(select(Order).where(Order.id == out.id))
    ).scalar_one()
    # reload with relationships for the builder
    from sqlalchemy.orm import selectinload
    order = (
        await db.execute(
            select(Order).options(selectinload(Order.items), selectinload(Order.cashier)).where(Order.id == out.id)
        )
    ).scalar_one()
    settings = (await db.execute(select(Settings))).scalar_one()
    receipt = build_sale_receipt(order, settings)
    assert receipt.totals.discount_percent == Decimal("10")
    assert receipt.totals.discount_amount == Decimal("100.00")
