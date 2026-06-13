"""Task 6 — checkout prices products with stone value and snapshots stone
value/cost onto OrderItem.

Uses the same direct-function-call pattern as test_order_discount.py (no
HTTP transport needed; avoids dependency-override boilerplate).

Math for the product (karat_markup=0, Settings default):
  purity_rate = 60 * 0.750 = 45.00
  metal_value = 45.00 * 10  = 450.00
  margin_amount = 450.00 * 20% = 90.00
  with_margin   = 450.00 + 90.00 = 540.00
  making_charge = 15.00
  stone_value   = 300.00
  final_price   = 540.00 + 15.00 + 300.00 = 855.00

cost_basis_usd = gold-only metal COGS (product.cost_basis_usd is None →
  falls back to gold_rate_at_sale * weight * purity * qty):
  = 60 * 10 * 0.75 * 1 = 450.00
"""
import pytest
from decimal import Decimal

from sqlalchemy import select

from app.api.orders import create_order
from app.models import (
    GoldRateOverride,
    Karat,
    OrderItem,
    Product,
    ProductStatus,
    Role,
    Settings,
    User,
)
from app.schemas.order import CheckoutRequest, OrderItemIn


async def _seed(db):
    admin = User(
        id="u-admin",
        email="a@x.com",
        name="A",
        password_hash="x",
        role=Role.ADMIN,
        is_active=True,
    )
    settings = Settings(
        id="singleton",
        accounting_auto_post_enabled=False,
        vat_percent=Decimal("0"),       # keep VAT=0 so total math stays simple
        lbp_exchange_rate=Decimal("1"),
        max_discount_percent=Decimal("0"),
        markup_k18=Decimal("0"),
        markup_k21=Decimal("0"),
        markup_k24=Decimal("0"),
    )
    # Fix the gold rate to 60 USD/g via an active override.
    override = GoldRateOverride(rate_24k=Decimal("60"), set_by="u-admin", is_active=True)
    product = Product(
        id="p1",
        code="FN-K18-9001",
        name_en="Diamond Ring",
        name_ar="",
        category="Rings",
        karat=Karat.K18,
        weight_grams=Decimal("10"),
        margin_percent=Decimal("20"),
        making_charge=Decimal("15"),
        on_hand_qty=3,
        status=ProductStatus.AVAILABLE,
        stone_value_usd=Decimal("300"),
        stone_cost_usd=Decimal("180"),
    )
    db.add_all([admin, settings, override, product])
    await db.commit()
    return admin


@pytest.mark.asyncio
async def test_checkout_diamond_product_includes_stone_value(db):
    admin = await _seed(db)

    payload = CheckoutRequest(
        payment_method="CASH",
        items=[OrderItemIn(item_kind="PRODUCT", product_id="p1", quantity=1)],
    )
    out = await create_order(payload, db=db, user=admin)
    assert out.subtotal == Decimal("855.00"), f"subtotal was {out.subtotal}"

    item = (
        await db.execute(select(OrderItem).where(OrderItem.product_id == "p1"))
    ).scalar_one()

    # gold body 60*0.75*10=450, +20%=540, +15 making=555, +300 stone = 855
    assert item.final_price == Decimal("855.00"), f"final_price was {item.final_price}"
    assert item.stone_value_at_sale == Decimal("300.00"), f"stone_value_at_sale was {item.stone_value_at_sale}"
    assert item.stone_cost_at_sale == Decimal("180.00"), f"stone_cost_at_sale was {item.stone_cost_at_sale}"
    # gold-only metal COGS, no stones: 60 * 0.75 * 10 = 450
    assert item.cost_basis_usd == Decimal("450.00"), f"cost_basis_usd was {item.cost_basis_usd}"
