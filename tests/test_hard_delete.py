"""Task 1 — hard-delete products and categories with reference guard + audit.

Four tests:
  1. Unreferenced product: DELETE ?hard=true → 204, row gone, PRODUCT_HARD_DELETED ledger row.
  2. Referenced product (has an order_items row): DELETE ?hard=true → 409, row still present.
  3. Unreferenced category: DELETE ?hard=true → 204, row gone, CATEGORY_HARD_DELETED ledger row.
  4. Category in use (has a product with category_id): DELETE ?hard=true → 409.
"""
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.models import (
    Category,
    InventoryLedger,
    Karat,
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


# ── shared fixture ─────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client(db):
    from app.main import app
    from app.deps import get_db, get_current_user

    admin = User(
        id="u-admin-hd", email="hd@x.com", name="HD Admin",
        password_hash="x", role=Role.ADMIN, is_active=True,
    )
    db.add(admin)
    await db.flush()

    async def _get_db():
        yield db

    async def _get_user():
        return admin

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = _get_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_product(pid: str, code: str, cat_id: str | None = None) -> Product:
    return Product(
        id=pid, code=code, name_en="Ring", name_ar="", category="Rings",
        category_id=cat_id, karat=Karat.K18,
        weight_grams=Decimal("5.000"), margin_percent=Decimal("15"),
        making_charge=Decimal("25"), on_hand_qty=1,
        status=ProductStatus.AVAILABLE,
    )


def _make_order_with_product(order_id: str, item_id: str, product_id: str, cashier_id: str) -> Order:
    return Order(
        id=order_id, order_number=f"ORD-HD-{order_id}", cashier_id=cashier_id,
        status=OrderStatus.COMPLETED, payment_method=PaymentMethod.CASH,
        subtotal=Decimal("500.00"), vat_percent=Decimal("11"),
        vat_amount=Decimal("55.00"), total_usd=Decimal("555.00"),
        total_lbp=Decimal("49672500.00"), lbp_exchange_rate=Decimal("89500"),
        items=[
            OrderItem(
                id=item_id, order_id=order_id, product_id=product_id,
                item_kind=OrderItemKind.PRODUCT,
                product_code="TST-18K-0001", product_name="Ring",
                karat=Karat.K18, weight_grams=Decimal("5.000"),
                gold_rate_at_sale=Decimal("80.00"),
                margin_percent=Decimal("15"), making_charge=Decimal("25"),
                final_price=Decimal("500.00"),
            )
        ],
    )


# ── test 1: unreferenced product hard-delete → 204 + row gone + ledger row ────

@pytest.mark.asyncio
async def test_hard_delete_unreferenced_product(client, db):
    product = _make_product("prod-hd-free", "TST-18K-HDFF")
    db.add(product)
    await db.commit()

    r = await client.delete("/api/products/prod-hd-free?hard=true")
    assert r.status_code == 204, r.text

    gone = (await db.execute(
        select(Product).where(Product.id == "prod-hd-free")
    )).scalar_one_or_none()
    assert gone is None, "Product row must be physically deleted"

    ledger = (await db.execute(
        select(InventoryLedger).where(
            InventoryLedger.ref_type == "product",
            InventoryLedger.ref_id == "prod-hd-free",
            InventoryLedger.event_type == "PRODUCT_HARD_DELETED",
        )
    )).scalar_one_or_none()
    assert ledger is not None, "PRODUCT_HARD_DELETED ledger row must exist"


# ── test 2: referenced product (has order_item) → 409, row still present ──────

@pytest.mark.asyncio
async def test_hard_delete_referenced_product_returns_409(client, db):
    product = _make_product("prod-hd-ref", "TST-18K-HDRF")
    db.add(product)
    await db.flush()

    order = _make_order_with_product(
        "ord-hd-01", "item-hd-01", "prod-hd-ref", "u-admin-hd"
    )
    db.add(order)
    await db.commit()

    r = await client.delete("/api/products/prod-hd-ref?hard=true")
    assert r.status_code == 409, r.text

    still_there = (await db.execute(
        select(Product).where(Product.id == "prod-hd-ref")
    )).scalar_one_or_none()
    assert still_there is not None, "Product must NOT be deleted when references exist"


# ── test 3: unreferenced category hard-delete → 204 + row gone + ledger row ───

@pytest.mark.asyncio
async def test_hard_delete_unreferenced_category(client, db):
    cat = Category(
        id="cat-hd-free", name_en="Bangles", name_ar="", slug="bangles-hd", is_active=True
    )
    db.add(cat)
    await db.commit()

    r = await client.delete("/api/categories/cat-hd-free?hard=true")
    assert r.status_code == 204, r.text

    gone = (await db.execute(
        select(Category).where(Category.id == "cat-hd-free")
    )).scalar_one_or_none()
    assert gone is None, "Category row must be physically deleted"

    ledger = (await db.execute(
        select(InventoryLedger).where(
            InventoryLedger.ref_type == "category",
            InventoryLedger.ref_id == "cat-hd-free",
            InventoryLedger.event_type == "CATEGORY_HARD_DELETED",
        )
    )).scalar_one_or_none()
    assert ledger is not None, "CATEGORY_HARD_DELETED ledger row must exist"


# ── test 4: category in use → 409 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hard_delete_category_in_use_returns_409(client, db):
    cat = Category(
        id="cat-hd-used", name_en="Necklaces", name_ar="", slug="necklaces-hd", is_active=True
    )
    db.add(cat)
    await db.flush()

    product = _make_product("prod-hd-incat", "TST-18K-HDIC", cat_id="cat-hd-used")
    db.add(product)
    await db.commit()

    r = await client.delete("/api/categories/cat-hd-used?hard=true")
    assert r.status_code == 409, r.text

    still_there = (await db.execute(
        select(Category).where(Category.id == "cat-hd-used")
    )).scalar_one_or_none()
    assert still_there is not None, "Category must NOT be deleted when products reference it"
