"""Task 13a — snapshotted stone value surfaces on the sale receipt line.

Mirrors the real-ORM-model approach used in test_receipt_builders.py (no stubs).
"""
from datetime import datetime, timezone
from decimal import Decimal

from app.core.receipt import build_sale_receipt
from app.models import (
    Karat,
    Order,
    OrderItem,
    OrderItemKind,
    OrderStatus,
    PaymentMethod,
    Settings,
    User,
)


NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _settings() -> Settings:
    return Settings(
        id="singleton",
        store_name="Fawaz El Namel",
        store_name_ar="فواز النمل",
        address="Beirut",
        phone="+961",
        vat_number="VAT-1",
        receipt_footer="Thank you",
        vat_percent=Decimal("11"),
        lbp_exchange_rate=Decimal("89500"),
    )


def _cashier() -> User:
    return User(id="u1", email="c@x.com", name="Cashier One", password_hash="x", role="CASHIER")


def test_sale_receipt_stone_value_propagated():
    """An item with stone_value_at_sale=300 → receipt line stone_value == 300."""
    cashier = _cashier()
    order = Order(
        id="o1",
        order_number="ORD-20260601-100",
        status=OrderStatus.COMPLETED,
        payment_method=PaymentMethod.CASH,
        customer_name="Test Customer",
        cashier_id="u1",
        cashier=cashier,
        subtotal=Decimal("1500.00"),
        vat_percent=Decimal("11"),
        vat_amount=Decimal("165.00"),
        total_usd=Decimal("1665.00"),
        total_lbp=Decimal("149017500.00"),
        lbp_exchange_rate=Decimal("89500"),
        created_at=NOW,
        items=[
            OrderItem(
                id="i1",
                order_id="o1",
                item_kind=OrderItemKind.PRODUCT,
                quantity=1,
                product_code="FN-RING-18K-0001",
                product_name="Diamond Ring 18K",
                karat=Karat.K18,
                weight_grams=Decimal("5.000"),
                gold_rate_at_sale=Decimal("60.00"),
                margin_percent=Decimal("20"),
                making_charge=Decimal("200.00"),
                final_price=Decimal("1500.00"),
                stone_value_at_sale=Decimal("300"),
                stone_cost_at_sale=Decimal("200"),
            )
        ],
    )

    r = build_sale_receipt(order, _settings())
    assert len(r.lines) == 1
    assert r.lines[0].stone_value == Decimal("300")


def test_sale_receipt_non_stone_item_unaffected():
    """An item with stone_value_at_sale=None → receipt line stone_value is None."""
    cashier = _cashier()
    order = Order(
        id="o2",
        order_number="ORD-20260601-101",
        status=OrderStatus.COMPLETED,
        payment_method=PaymentMethod.CASH,
        customer_name=None,
        cashier_id="u1",
        cashier=cashier,
        subtotal=Decimal("800.00"),
        vat_percent=Decimal("11"),
        vat_amount=Decimal("88.00"),
        total_usd=Decimal("888.00"),
        total_lbp=Decimal("79476000.00"),
        lbp_exchange_rate=Decimal("89500"),
        created_at=NOW,
        items=[
            OrderItem(
                id="i2",
                order_id="o2",
                item_kind=OrderItemKind.PRODUCT,
                quantity=1,
                product_code="FN-BANGLE-22K-0001",
                product_name="Plain Bangle 22K",
                karat=Karat.K22,
                weight_grams=Decimal("10.000"),
                gold_rate_at_sale=Decimal("80.00"),
                margin_percent=Decimal("10"),
                making_charge=Decimal("0"),
                final_price=Decimal("800.00"),
                stone_value_at_sale=None,
                stone_cost_at_sale=None,
            )
        ],
    )

    r = build_sale_receipt(order, _settings())
    assert len(r.lines) == 1
    assert r.lines[0].stone_value is None
