"""Phase 4 — receipt endpoints return the normalized ReceiptOut for all three
sources (sales, supplier purchase, buyback). Checkpoints 4.1, 4.2, 4.3."""
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.api.buybacks import get_buyback_receipt
from app.api.orders import create_order, get_order_receipt
from app.api.suppliers import get_purchase_receipt
from app.models import (
    BuybackKind,
    BuybackPriceMode,
    CoinType,
    GoldRateOverride,
    Karat,
    MarginMode,
    Role,
    Settings,
    Supplier,
    SupplierItemKind,
    SupplierPurchase,
    SupplierPurchaseItem,
    SupplierPurchaseMode,
    User,
    WalkinBuyback,
)
from app.schemas.order import CheckoutRequest, OrderItemIn
from app.schemas.receipt import ReceiptType


async def _common(db):
    admin = User(id="admin1", email="a@x.com", name="Admin", password_hash="x", role=Role.ADMIN)
    settings = Settings(id="singleton", store_name="MAISON ZAHAB", vat_percent=Decimal("11"),
                        lbp_exchange_rate=Decimal("89500"), max_discount_percent=Decimal("0"))
    db.add_all([admin, settings])
    await db.commit()
    return admin


@pytest.mark.asyncio
async def test_4_1_sales_receipt_shows_customer_and_cashier(db):
    admin = await _common(db)
    coin = CoinType(id="c1", code="COIN1", name_en="Lira", karat="K22", weight_grams=Decimal("5"),
                    markup_per_gram=Decimal("0"), margin_mode=MarginMode.USD, margin_value=Decimal("0"),
                    on_hand_qty=10)
    db.add(coin)
    db.add(GoldRateOverride(rate_24k=Decimal("100"), set_by="admin1", is_active=True))
    await db.commit()
    out = await create_order(
        CheckoutRequest(items=[OrderItemIn(item_kind="COIN", coin_type_id="c1", quantity=1)],
                        payment_method="CASH", customer_name="Jane Doe"),
        db=db, user=admin,
    )
    receipt = await get_order_receipt(out.id, db=db, _=admin)
    assert receipt.type == ReceiptType.SALE
    assert receipt.cashier_name == "Admin"
    assert receipt.party.role == "customer" and receipt.party.name == "Jane Doe"
    assert receipt.store.name == "MAISON ZAHAB"


@pytest.mark.asyncio
async def test_4_2_supplier_receipt_lists_items_and_supplier(db):
    admin = await _common(db)
    supplier = Supplier(id="s1", name="Gold Supplier Co")
    purchase = SupplierPurchase(
        id="sp1", supplier_id="s1", payment_mode=SupplierPurchaseMode.CASH,
        created_by_user_id="admin1",
        items=[
            SupplierPurchaseItem(id="it1", purchase_id="sp1", item_kind=SupplierItemKind.COIN,
                                 coin_type_id="c1", quantity=5, karat=Karat.K22,
                                 weight_grams=Decimal("8"), unit_cost_usd=Decimal("600")),
        ],
    )
    coin = CoinType(id="c1", code="COIN1", name_en="Lira Coin", karat="K22", weight_grams=Decimal("8"),
                    markup_per_gram=Decimal("0"), margin_mode=MarginMode.USD, margin_value=Decimal("0"),
                    on_hand_qty=5)
    db.add_all([supplier, coin, purchase])
    await db.commit()

    receipt = await get_purchase_receipt("sp1", db=db, _=admin)
    assert receipt.type == ReceiptType.SUPPLIER_PURCHASE
    assert receipt.party.role == "supplier" and receipt.party.name == "Gold Supplier Co"
    assert len(receipt.lines) == 1
    assert receipt.lines[0].description == "Lira Coin"  # resolved name
    assert receipt.totals.total_usd == Decimal("3000.00")  # 600 × 5


@pytest.mark.asyncio
async def test_4_3_buyback_receipt(db):
    admin = await _common(db)
    bb = WalkinBuyback(
        id="bb1", seller_name="Walk-in", seller_phone="+961", cashier_id="admin1",
        kind=BuybackKind.PURE_GOLD, karat=Karat.K21, weight_grams=Decimal("20"),
        quantity=1, buy_price_usd=Decimal("1200"), gold_rate_at_buy=Decimal("80"),
        price_mode=BuybackPriceMode.FORMULA,
    )
    db.add(bb)
    await db.commit()

    receipt = await get_buyback_receipt("bb1", db=db, _=admin)
    assert receipt.type == ReceiptType.BUYBACK
    assert receipt.party.role == "seller" and receipt.party.name == "Walk-in"
    assert receipt.cashier_name == "Admin"
    assert receipt.totals.total_usd == Decimal("1200.00")
