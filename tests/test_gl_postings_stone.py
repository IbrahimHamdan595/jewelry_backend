"""GL stone COGS posting tests.

Verifies that a diamond/stone-bearing product sale posts balanced GL entries
with STONE_COGS / STONE_INVENTORY lines, and that a plain gold product sale
posts no stone lines.

GL model quick reference (confirmed from app/core/gl.py and app/models):
- Entry ORM:   GLJournalEntry  (source_type == "ORDER" for sales)
- Line ORM:    GLJournalLine   (FK: entry_id)
- Account ORM: GLAccount       (field: system_key)
- Line fields: base_debit, base_credit, account_id
"""
import pytest
import pytest_asyncio
from decimal import Decimal as D
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.coa_seed import seed_chart_of_accounts
from app.models import (
    GLAccount, GLJournalEntry, GLJournalLine, GLPeriod, PeriodStatus,
    Settings, User, Role,
)


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
        name_en="Ring",
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
async def test_diamond_sale_posts_balanced_with_stone_cogs(client, db):
    """A stone product sale posts a balanced entry + STONE_COGS/STONE_INVENTORY pair."""
    await _seed_rate_and_product(db, stone_value_usd=D("300"), stone_cost_usd=D("180"))

    r = await client.post("/api/orders", json={
        "payment_method": "CASH",
        "items": [{"item_kind": "PRODUCT", "product_id": "p1", "quantity": 1}],
    })
    assert r.status_code in (200, 201), r.text

    # Load the ORDER journal entry
    entry = (
        await db.execute(
            select(GLJournalEntry).where(GLJournalEntry.source_type == "ORDER")
        )
    ).scalars().first()
    assert entry is not None, "Expected an ORDER GLJournalEntry"

    # Load all lines for this entry
    lines = (
        await db.execute(
            select(GLJournalLine).where(GLJournalLine.entry_id == entry.id)
        )
    ).scalars().all()
    assert lines, "Expected journal lines"

    # Money dimension must balance
    total_debit = sum(ln.base_debit for ln in lines)
    total_credit = sum(ln.base_credit for ln in lines)
    assert total_debit == total_credit, (
        f"Entry is unbalanced: base_debit={total_debit} != base_credit={total_credit}"
    )

    # Build account_id → system_key map
    all_accounts = (await db.execute(select(GLAccount))).scalars().all()
    key_by_id = {a.id: a.system_key for a in all_accounts}

    # Find STONE_COGS and STONE_INVENTORY lines
    stone_cogs_lines = [ln for ln in lines if key_by_id.get(ln.account_id) == "STONE_COGS"]
    stone_inv_lines  = [ln for ln in lines if key_by_id.get(ln.account_id) == "STONE_INVENTORY"]

    assert len(stone_cogs_lines) == 1, (
        f"Expected exactly 1 STONE_COGS line, got {len(stone_cogs_lines)}"
    )
    assert len(stone_inv_lines) == 1, (
        f"Expected exactly 1 STONE_INVENTORY line, got {len(stone_inv_lines)}"
    )

    assert stone_cogs_lines[0].base_debit == D("180.00"), (
        f"STONE_COGS base_debit should be 180, got {stone_cogs_lines[0].base_debit}"
    )
    assert stone_inv_lines[0].base_credit == D("180.00"), (
        f"STONE_INVENTORY base_credit should be 180, got {stone_inv_lines[0].base_credit}"
    )


@pytest.mark.asyncio
async def test_plain_gold_sale_posts_no_stone_lines(client, db):
    """A plain gold product sale (no stone fields) posts no STONE_COGS/STONE_INVENTORY lines."""
    await _seed_rate_and_product(db)  # no stone kwargs

    r = await client.post("/api/orders", json={
        "payment_method": "CASH",
        "items": [{"item_kind": "PRODUCT", "product_id": "p1", "quantity": 1}],
    })
    assert r.status_code in (200, 201), r.text

    entry = (
        await db.execute(
            select(GLJournalEntry).where(GLJournalEntry.source_type == "ORDER")
        )
    ).scalars().first()
    assert entry is not None, "Expected an ORDER GLJournalEntry"

    lines = (
        await db.execute(
            select(GLJournalLine).where(GLJournalLine.entry_id == entry.id)
        )
    ).scalars().all()

    all_accounts = (await db.execute(select(GLAccount))).scalars().all()
    key_by_id = {a.id: a.system_key for a in all_accounts}

    stone_cogs_lines = [ln for ln in lines if key_by_id.get(ln.account_id) == "STONE_COGS"]
    stone_inv_lines  = [ln for ln in lines if key_by_id.get(ln.account_id) == "STONE_INVENTORY"]

    assert stone_cogs_lines == [], (
        f"Expected no STONE_COGS lines for plain gold sale, got {len(stone_cogs_lines)}"
    )
    assert stone_inv_lines == [], (
        f"Expected no STONE_INVENTORY lines for plain gold sale, got {len(stone_inv_lines)}"
    )
