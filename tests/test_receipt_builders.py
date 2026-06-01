"""Phase 0 checkpoint 0.2 — the shared ReceiptOut shape validates against one
existing order, one supplier purchase, and one buyback. Builders are pure, so
these construct in-memory ORM rows (no DB) and assert the normalized output."""
from datetime import datetime, timezone
from decimal import Decimal

from app.core.receipt import (
    build_buyback_receipt,
    build_sale_receipt,
    build_supplier_receipt,
)
from app.models import (
    BuybackKind,
    BuybackPriceMode,
    Karat,
    Order,
    OrderItem,
    OrderItemKind,
    OrderStatus,
    PaymentMethod,
    Settings,
    SupplierItemKind,
    SupplierPurchase,
    SupplierPurchaseItem,
    SupplierPurchaseMode,
    User,
    WalkinBuyback,
)
from app.schemas.receipt import ReceiptType

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _settings() -> Settings:
    return Settings(
        id="singleton",
        store_name="MAISON ZAHAB",
        store_name_ar="ميزون ذهب",
        address="Beirut",
        phone="+961",
        vat_number="VAT-1",
        receipt_footer="Thank you",
        vat_percent=Decimal("11"),
        lbp_exchange_rate=Decimal("89500"),
    )


def test_sale_receipt_shape():
    cashier = User(id="u1", email="c@x.com", name="Cashier One", password_hash="x", role="CASHIER")
    order = Order(
        id="o1",
        order_number="ORD-20260601-001",
        status=OrderStatus.COMPLETED,
        payment_method=PaymentMethod.CASH,
        customer_name="Jane Doe",
        cashier_id="u1",
        cashier=cashier,
        subtotal=Decimal("1000.00"),
        vat_percent=Decimal("11"),
        vat_amount=Decimal("110.00"),
        total_usd=Decimal("1110.00"),
        total_lbp=Decimal("99345000.00"),
        lbp_exchange_rate=Decimal("89500"),
        created_at=NOW,
        items=[
            OrderItem(
                id="i1", order_id="o1", item_kind=OrderItemKind.COIN, quantity=2,
                product_code="MZB-COIN-22K-0001", product_name="Lira Coin",
                karat=Karat.K22, weight_grams=Decimal("8.000"),
                gold_rate_at_sale=Decimal("80.00"), margin_percent=Decimal("0"),
                making_charge=Decimal("0"), final_price=Decimal("1000.00"),
            )
        ],
    )

    r = build_sale_receipt(order, _settings())
    assert r.type == ReceiptType.SALE
    assert r.reference == "ORD-20260601-001"
    assert r.cashier_name == "Cashier One"
    assert r.party.role == "customer" and r.party.name == "Jane Doe"
    assert r.store.name == "MAISON ZAHAB"
    assert len(r.lines) == 1
    assert r.lines[0].quantity == Decimal("2")
    assert r.lines[0].unit_price == Decimal("500.00")  # 1000 / 2
    assert r.lines[0].line_total == Decimal("1000.00")
    assert r.totals.vat_amount == Decimal("110.00")
    assert r.totals.total_usd == Decimal("1110.00")
    assert r.payment_method == "CASH"


def test_supplier_purchase_receipt_shape():
    purchase = SupplierPurchase(
        id="sp1",
        supplier_id="s1",
        occurred_at=NOW,
        payment_mode=SupplierPurchaseMode.CASH,
        created_by_user_id="u1",
        items=[
            SupplierPurchaseItem(
                id="spi1", purchase_id="sp1", item_kind=SupplierItemKind.PURE_GOLD,
                weight_grams=Decimal("50.000"), karat=Karat.K21, quantity=1,
                unit_cost_usd=Decimal("3000.00"),
            ),
            SupplierPurchaseItem(
                id="spi2", purchase_id="sp1", item_kind=SupplierItemKind.COIN,
                weight_grams=Decimal("8.000"), karat=Karat.K22, quantity=5,
                unit_cost_usd=Decimal("600.00"),
            ),
        ],
    )

    r = build_supplier_receipt(
        purchase, _settings(),
        supplier_name="Gold Supplier Co",
        item_descriptions={"spi1": "Pure gold bar", "spi2": "Lira coins"},
    )
    assert r.type == ReceiptType.SUPPLIER_PURCHASE
    assert r.reference == "sp1"
    assert r.party.role == "supplier" and r.party.name == "Gold Supplier Co"
    assert len(r.lines) == 2
    assert r.lines[0].description == "Pure gold bar"
    # subtotal = 3000*1 + 600*5 = 6000
    assert r.totals.subtotal == Decimal("6000.00")
    assert r.totals.total_usd == Decimal("6000.00")
    assert r.totals.vat_amount is None


def test_buyback_receipt_shape():
    buyback = WalkinBuyback(
        id="bb1",
        occurred_at=NOW,
        seller_name="Walk-in Seller",
        seller_phone="+961 70 000000",
        cashier_id="u1",
        kind=BuybackKind.PURE_GOLD,
        karat=Karat.K21,
        weight_grams=Decimal("20.000"),
        quantity=1,
        buy_price_usd=Decimal("1200.00"),
        gold_rate_at_buy=Decimal("80.00"),
        price_mode=BuybackPriceMode.FORMULA,
    )

    r = build_buyback_receipt(buyback, _settings(), item_description="Pure gold 21K")
    assert r.type == ReceiptType.BUYBACK
    assert r.reference == "bb1"
    assert r.party.role == "seller" and r.party.phone == "+961 70 000000"
    assert len(r.lines) == 1
    assert r.lines[0].line_total == Decimal("1200.00")
    assert r.totals.total_usd == Decimal("1200.00")


def test_sale_receipt_serializes_to_json():
    # The frontend consumes JSON — ensure Decimals/datetimes serialize cleanly.
    cashier = User(id="u1", email="c@x.com", name="C", password_hash="x", role="CASHIER")
    order = Order(
        id="o1", order_number="ORD-1", status=OrderStatus.COMPLETED,
        payment_method=PaymentMethod.CARD, customer_name=None, cashier_id="u1",
        cashier=cashier, subtotal=Decimal("100"), vat_percent=Decimal("11"),
        vat_amount=Decimal("11"), total_usd=Decimal("111"), total_lbp=Decimal("0"),
        lbp_exchange_rate=Decimal("89500"), created_at=NOW, items=[],
    )
    r = build_sale_receipt(order, _settings())
    blob = r.model_dump_json()
    assert "ORD-1" in blob
