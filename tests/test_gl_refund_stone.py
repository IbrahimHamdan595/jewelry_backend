"""GL stone COGS reversal on partial diamond refunds.

Verifies that a partial per-item refund of a diamond product posts a balanced
ORDER_REFUND GL entry that includes:
  - STONE_INVENTORY  base_debit  == stone_cost_at_sale (180.00)
  - STONE_COGS       base_credit == stone_cost_at_sale (180.00)

GL model quick reference (confirmed from app/core/gl.py and app/models):
- Entry ORM:   GLJournalEntry  (source_type == "ORDER_REFUND" for per-item refunds)
- Line ORM:    GLJournalLine   (FK: entry_id)
- Account ORM: GLAccount       (field: system_key)
- Line fields: base_debit, base_credit, account_id
"""
import pytest
import pytest_asyncio
from decimal import Decimal as D
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.orders import refund_order_item
from app.core.coa_seed import seed_chart_of_accounts
from app.models import (
    GLAccount, GLJournalEntry, GLJournalLine, GLPeriod, PeriodStatus,
    Settings, User, Role,
)
from app.schemas.order import ItemRefundRequest


@pytest_asyncio.fixture
async def client(db):
    from app.main import app
    from app.deps import get_db, get_current_user

    admin = User(id="u-admin", email="a@x.com", name="A", password_hash="x", role=Role.ADMIN, is_active=True)
    db.add(admin)
    db.add(Settings(id="singleton", accounting_auto_post_enabled=True))
    await seed_chart_of_accounts(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    await db.flush()

    async def _get_db():
        yield db

    async def _get_user():
        return admin

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = _get_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _seed_rate_and_product(db, **stone):
    from app.models import Product, Karat, ProductStatus, GoldRateHistory

    db.add(GoldRateHistory(rate_24k=D("60"), source="test"))
    p = Product(
        id="p1",
        code="FN-K18-9001",
        name_en="DiamondRing",
        name_ar="",
        category="Rings",
        karat=Karat.K18,
        weight_grams=D("10"),
        margin_percent=D("20"),
        making_charge=D("15"),
        on_hand_qty=3,
        status=ProductStatus.AVAILABLE,
        **stone,
    )
    db.add(p)
    await db.flush()


@pytest.mark.asyncio
async def test_partial_refund_reverses_stone_cogs(client, db):
    """Refunding qty 1 of a diamond product creates an ORDER_REFUND entry
    with balanced lines including STONE_INVENTORY debit and STONE_COGS credit
    each equal to 180.00."""
    await _seed_rate_and_product(db, stone_value_usd=D("300"), stone_cost_usd=D("180"))

    # Checkout: creates the ORDER GL entry via auto-post
    r = await client.post("/api/orders", json={
        "payment_method": "CASH",
        "items": [{"item_kind": "PRODUCT", "product_id": "p1", "quantity": 1}],
    })
    assert r.status_code in (200, 201), r.text
    order_data = r.json()
    order_id = order_data["id"]
    item_id = order_data["items"][0]["id"]

    # Load admin user for direct function call
    admin = (await db.execute(select(User).where(User.id == "u-admin"))).scalar_one()

    # Trigger a partial refund of qty 1 for this product line
    await refund_order_item(order_id, item_id, ItemRefundRequest(), db=db, user=admin)

    # Load the ORDER_REFUND journal entry
    entry = (
        await db.execute(
            select(GLJournalEntry).where(GLJournalEntry.source_type == "ORDER_REFUND")
        )
    ).scalars().first()
    assert entry is not None, "Expected an ORDER_REFUND GLJournalEntry"

    # Load all lines for this entry
    lines = (
        await db.execute(
            select(GLJournalLine).where(GLJournalLine.entry_id == entry.id)
        )
    ).scalars().all()
    assert lines, "Expected journal lines on the refund entry"

    # Entry must balance (sum of base_debit == sum of base_credit)
    total_debit = sum(ln.base_debit for ln in lines)
    total_credit = sum(ln.base_credit for ln in lines)
    assert total_debit == total_credit, (
        f"Refund entry is unbalanced: base_debit={total_debit} != base_credit={total_credit}"
    )

    # Build account_id → system_key map
    all_accounts = (await db.execute(select(GLAccount))).scalars().all()
    key_by_id = {a.id: a.system_key for a in all_accounts}

    # Find STONE_INVENTORY and STONE_COGS lines on the refund entry
    stone_inv_lines  = [ln for ln in lines if key_by_id.get(ln.account_id) == "STONE_INVENTORY"]
    stone_cogs_lines = [ln for ln in lines if key_by_id.get(ln.account_id) == "STONE_COGS"]

    assert len(stone_inv_lines) == 1, (
        f"Expected exactly 1 STONE_INVENTORY line on refund, got {len(stone_inv_lines)}"
    )
    assert len(stone_cogs_lines) == 1, (
        f"Expected exactly 1 STONE_COGS line on refund, got {len(stone_cogs_lines)}"
    )

    assert stone_inv_lines[0].base_debit == D("180.00"), (
        f"STONE_INVENTORY base_debit should be 180.00, got {stone_inv_lines[0].base_debit}"
    )
    assert stone_cogs_lines[0].base_credit == D("180.00"), (
        f"STONE_COGS base_credit should be 180.00, got {stone_cogs_lines[0].base_credit}"
    )
