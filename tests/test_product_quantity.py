"""Phase 3 — product quantity attribute. Checkpoints 3.2–3.6 plus zakat (3.3).

Products are now stocked-by-quantity: checkout decrements on_hand_qty, refunds
return units, overselling is a 409, and zakat multiplies weight × on_hand_qty."""
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.orders import create_order, refund_order_item
from app.core.zakat import _compute_holdings_from_rows, compute_zakatable_holdings
from app.models import (
    GoldRateOverride,
    Karat,
    Order,
    Product,
    ProductStatus,
    Role,
    Settings,
    User,
)
from app.schemas.order import CheckoutRequest, ItemRefundRequest, OrderItemIn


async def _seed(db, *, on_hand=5):
    admin = User(id="admin1", email="a@x.com", name="Admin", password_hash="x", role=Role.ADMIN)
    settings = Settings(id="singleton", vat_percent=Decimal("0"), lbp_exchange_rate=Decimal("1"),
                        max_discount_percent=Decimal("0"))
    # weight 1g, rate 100, margin 0, making 0 → unit price 100 (purity K24 0.999 → 99.9). Use K24.
    product = Product(
        id="p1", code="FN-24K-0001", name_en="Bar", category="bars", karat=Karat.K24,
        weight_grams=Decimal("1.000"), margin_percent=Decimal("0"), making_charge=Decimal("0"),
        on_hand_qty=on_hand, status=ProductStatus.AVAILABLE,
    )
    override = GoldRateOverride(rate_24k=Decimal("100"), set_by="admin1", is_active=True)
    db.add_all([admin, settings, product, override])
    await db.commit()
    return admin


@pytest.mark.asyncio
async def test_3_2_selling_3_decrements_by_3_and_refund_1_returns_1(db):
    admin = await _seed(db, on_hand=5)
    out = await create_order(
        CheckoutRequest(
            items=[OrderItemIn(item_kind="PRODUCT", product_id="p1", quantity=3)],
            payment_method="CASH",
        ),
        db=db, user=admin,
    )
    product = (await db.execute(select(Product).where(Product.id == "p1"))).scalar_one()
    assert product.on_hand_qty == 2  # 5 − 3
    assert product.status == ProductStatus.AVAILABLE
    assert out.items[0].quantity == 3

    # Refund 1 of the 3 sold → exactly 1 returns to stock.
    await refund_order_item(out.id, out.items[0].id, ItemRefundRequest(quantity=1), db=db, user=admin)
    product = (await db.execute(select(Product).where(Product.id == "p1"))).scalar_one()
    assert product.on_hand_qty == 3  # 2 + 1


@pytest.mark.asyncio
async def test_3_6_overselling_is_409(db):
    admin = await _seed(db, on_hand=2)
    with pytest.raises(HTTPException) as exc:
        await create_order(
            CheckoutRequest(
                items=[OrderItemIn(item_kind="PRODUCT", product_id="p1", quantity=3)],
                payment_method="CASH",
            ),
            db=db, user=admin,
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_3_selling_all_sets_status_sold(db):
    admin = await _seed(db, on_hand=2)
    await create_order(
        CheckoutRequest(
            items=[OrderItemIn(item_kind="PRODUCT", product_id="p1", quantity=2)],
            payment_method="CASH",
        ),
        db=db, user=admin,
    )
    product = (await db.execute(select(Product).where(Product.id == "p1"))).scalar_one()
    assert product.on_hand_qty == 0
    assert product.status == ProductStatus.SOLD


@pytest.mark.asyncio
async def test_3_3_zakat_multiplies_weight_by_qty(db):
    # Pure aggregator: a product with qty=3 contributes weight×3.
    holdings_qty3 = _compute_holdings_from_rows(
        products=[{"karat": Karat.K24, "weight_grams": Decimal("10.000"), "on_hand_qty": 3}],
        coins=[], ounces=[], lots=[],
    )
    k24 = next(b for b in holdings_qty3.by_karat if b.karat == Karat.K24)
    assert k24.total_weight_grams == Decimal("30.000")  # 10 × 3

    # Regression guard: qty=1 (or omitted) is unchanged.
    holdings_qty1 = _compute_holdings_from_rows(
        products=[{"karat": Karat.K24, "weight_grams": Decimal("10.000"), "on_hand_qty": 1}],
        coins=[], ounces=[], lots=[],
    )
    k24_1 = next(b for b in holdings_qty1.by_karat if b.karat == Karat.K24)
    assert k24_1.total_weight_grams == Decimal("10.000")

    holdings_default = _compute_holdings_from_rows(
        products=[{"karat": Karat.K24, "weight_grams": Decimal("10.000")}],  # no on_hand_qty
        coins=[], ounces=[], lots=[],
    )
    k24_d = next(b for b in holdings_default.by_karat if b.karat == Karat.K24)
    assert k24_d.total_weight_grams == Decimal("10.000")


@pytest.mark.asyncio
async def test_3_3b_zakat_db_query_uses_qty(db):
    admin = await _seed(db, on_hand=4)  # 4 × 1g K24 product on hand
    holdings = await compute_zakatable_holdings(db)
    k24 = next(b for b in holdings.by_karat if b.karat == Karat.K24)
    assert k24.grams_by_source["products"] == Decimal("4.000")  # 1g × 4


@pytest.mark.asyncio
async def test_3_4_low_stock_alert_includes_products(db):
    from app.api.inventory import inventory_alerts

    admin = await _seed(db, on_hand=1)
    product = (await db.execute(select(Product).where(Product.id == "p1"))).scalar_one()
    product.min_stock_qty = 2  # 1 on hand <= 2 threshold → should alert
    await db.commit()

    result = await inventory_alerts(db=db, _=admin)
    product_alerts = [r for r in result["below_threshold"] if r["kind"] == "PRODUCT"]
    assert len(product_alerts) == 1
    assert product_alerts[0]["code"] == "FN-24K-0001"
    assert product_alerts[0]["on_hand_qty"] == 1
