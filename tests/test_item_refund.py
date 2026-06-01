"""Phase 1 — per-item refunds. Exercises the endpoint functions directly against
the in-memory DB fixture (mirrors the existing test style). Covers checkpoints
1.1–1.6."""
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api.orders import refund_order_item, void_order
from app.core.ledger import EVENT_ORDER_ITEM_REFUND
from app.models import (
    CoinType,
    InventoryLedger,
    Karat,
    MarginMode,
    Order,
    OrderItem,
    OrderItemKind,
    OrderStatus,
    PaymentMethod,
    Product,
    ProductStatus,
    Role,
    User,
)
from app.schemas.order import ItemRefundRequest, VoidRequest


async def _seed(db):
    """One order with a PRODUCT line ($500, qty 1) and a COIN line ($100×5).
    Subtotal 1000, VAT 11% = 110, total 1110."""
    admin = User(id="admin1", email="a@x.com", name="Admin", password_hash="x", role=Role.ADMIN)
    product = Product(
        id="p1", code="MZB-21K-0001", name_en="Ring", category="rings", karat=Karat.K21,
        weight_grams=Decimal("5.000"), margin_percent=Decimal("15"), making_charge=Decimal("25"),
        status=ProductStatus.SOLD, on_hand_qty=0,  # sold the single piece on this order
    )
    coin = CoinType(
        id="c1", code="MZB-COIN-22K-0001", name_en="Lira", karat=Karat.K22,
        weight_grams=Decimal("8.000"), margin_mode=MarginMode.USD, margin_value=Decimal("10"),
        on_hand_qty=5,  # 10 in stock, 5 already sold on this order
    )
    order = Order(
        id="o1", order_number="ORD-1", status=OrderStatus.COMPLETED,
        payment_method=PaymentMethod.CASH, cashier_id="admin1",
        subtotal=Decimal("1000.00"), vat_percent=Decimal("11"), vat_amount=Decimal("110.00"),
        total_usd=Decimal("1110.00"), total_lbp=Decimal("99345000.00"),
        lbp_exchange_rate=Decimal("89500"),
        items=[
            OrderItem(
                id="it_prod", order_id="o1", item_kind=OrderItemKind.PRODUCT, product_id="p1",
                quantity=1, product_code="MZB-21K-0001", product_name="Ring", karat=Karat.K21,
                weight_grams=Decimal("5.000"), gold_rate_at_sale=Decimal("80"),
                margin_percent=Decimal("15"), making_charge=Decimal("25"), final_price=Decimal("500.00"),
            ),
            OrderItem(
                id="it_coin", order_id="o1", item_kind=OrderItemKind.COIN, coin_type_id="c1",
                quantity=5, product_code="MZB-COIN-22K-0001", product_name="Lira", karat=Karat.K22,
                weight_grams=Decimal("8.000"), gold_rate_at_sale=Decimal("80"),
                margin_percent=Decimal("0"), making_charge=Decimal("0"), final_price=Decimal("500.00"),
            ),
        ],
    )
    db.add_all([admin, product, coin, order])
    await db.commit()
    return admin


@pytest.mark.asyncio
async def test_1_1_refund_product_line_flips_only_that_product(db):
    admin = await _seed(db)
    await refund_order_item("o1", "it_prod", ItemRefundRequest(), db=db, user=admin)

    product = (await db.execute(select(Product).where(Product.id == "p1"))).scalar_one()
    coin = (await db.execute(select(CoinType).where(CoinType.id == "c1"))).scalar_one()
    assert product.status == ProductStatus.AVAILABLE  # flipped back
    assert coin.on_hand_qty == 5  # untouched
    order = (await db.execute(select(Order).where(Order.id == "o1"))).scalar_one()
    assert order.status == OrderStatus.PARTIALLY_REFUNDED


@pytest.mark.asyncio
async def test_1_2_partial_coin_refund_returns_exact_qty(db):
    admin = await _seed(db)
    await refund_order_item("o1", "it_coin", ItemRefundRequest(quantity=2), db=db, user=admin)

    coin = (await db.execute(select(CoinType).where(CoinType.id == "c1"))).scalar_one()
    item = (await db.execute(select(OrderItem).where(OrderItem.id == "it_coin"))).scalar_one()
    assert coin.on_hand_qty == 7  # 5 + 2 returned
    assert item.refunded_qty == 2
    assert item.refunded_amount == Decimal("200.00")  # (500/5)*2


@pytest.mark.asyncio
async def test_1_3_totals_recompute_with_vat_recalculated(db):
    admin = await _seed(db)
    # Refund the $500 product line. Remaining subtotal = 500; VAT must be
    # recalculated to 55.00 (NOT 110 − 55 = 55 by coincidence — verify on coin too).
    await refund_order_item("o1", "it_prod", ItemRefundRequest(), db=db, user=admin)
    order = (await db.execute(select(Order).where(Order.id == "o1"))).scalar_one()
    assert order.subtotal == Decimal("500.00")
    assert order.vat_amount == Decimal("55.00")          # 500 * 11%
    assert order.total_usd == Decimal("555.00")
    assert order.total_lbp == Decimal("49672500.00")     # 555 * 89500

    # Now refund 2 of 5 coins ($200). Remaining = 300; VAT = 33.00.
    await refund_order_item("o1", "it_coin", ItemRefundRequest(quantity=2), db=db, user=admin)
    order = (await db.execute(select(Order).where(Order.id == "o1"))).scalar_one()
    assert order.subtotal == Decimal("300.00")
    assert order.vat_amount == Decimal("33.00")
    assert order.total_usd == Decimal("333.00")


@pytest.mark.asyncio
async def test_1_4_refunding_last_open_line_sets_refunded(db):
    admin = await _seed(db)
    await refund_order_item("o1", "it_prod", ItemRefundRequest(), db=db, user=admin)
    await refund_order_item("o1", "it_coin", ItemRefundRequest(quantity=5), db=db, user=admin)
    order = (await db.execute(select(Order).where(Order.id == "o1"))).scalar_one()
    assert order.status == OrderStatus.REFUNDED
    assert order.subtotal == Decimal("0.00")
    assert order.vat_amount == Decimal("0.00")


@pytest.mark.asyncio
async def test_1_5_one_ledger_event_per_refund_and_void_blocked(db):
    admin = await _seed(db)
    await refund_order_item("o1", "it_coin", ItemRefundRequest(quantity=2), db=db, user=admin)
    await refund_order_item("o1", "it_coin", ItemRefundRequest(quantity=1), db=db, user=admin)
    n = (
        await db.execute(
            select(func.count(InventoryLedger.id)).where(
                InventoryLedger.event_type == EVENT_ORDER_ITEM_REFUND
            )
        )
    ).scalar_one()
    assert n == 2  # one event per refund call

    # Voiding a partially-refunded order is blocked.
    with pytest.raises(HTTPException) as exc:
        await void_order("o1", VoidRequest(reason="x"), db=db, user=admin)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_1_5b_cannot_over_refund_a_line(db):
    admin = await _seed(db)
    await refund_order_item("o1", "it_coin", ItemRefundRequest(quantity=5), db=db, user=admin)
    with pytest.raises(HTTPException) as exc:  # nothing left
        await refund_order_item("o1", "it_coin", ItemRefundRequest(quantity=1), db=db, user=admin)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_1_5c_refund_more_than_remaining_is_409(db):
    admin = await _seed(db)
    with pytest.raises(HTTPException) as exc:
        await refund_order_item("o1", "it_coin", ItemRefundRequest(quantity=6), db=db, user=admin)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_1_6_non_admin_rejected(db):
    """require_admin gates the route — a cashier token yields 403."""
    from app.core.permissions import require_admin

    cashier = User(id="cash1", email="c@x.com", name="Cash", password_hash="x", role=Role.CASHIER)
    with pytest.raises(HTTPException) as exc:
        require_admin(user=cashier)
    assert exc.value.status_code == 403
