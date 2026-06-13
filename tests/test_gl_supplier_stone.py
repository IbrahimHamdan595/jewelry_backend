"""Task 9: supplier-intake stone split.

Tests that post_supplier_purchase carves the stone_cost_usd portion into
STONE_INVENTORY and leaves the remainder in PRODUCT_INVENTORY, keeping the
entry balanced.  Also covers the regression: a non-diamond product posts
the full amount to PRODUCT_INVENTORY with no STONE_INVENTORY line.
"""
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core import gl_postings as glp
from app.core.coa_seed import seed_chart_of_accounts
from app.models import (
    GLJournalLine, GLAccount,
    GLPeriod, PeriodStatus, Settings,
    Supplier, SupplierPurchase, SupplierPurchaseItem,
    SupplierPurchaseMode, SupplierItemKind,
    Product, Karat,
)

D = Decimal


async def _seeded(db):
    await seed_chart_of_accounts(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    await db.flush()


def _settings(on=True):
    return Settings(id="singleton", accounting_auto_post_enabled=on)


@pytest.mark.asyncio
async def test_diamond_product_splits_stone_inventory(db):
    """DR STONE_INVENTORY 200, DR PRODUCT_INVENTORY 300, CR AP 500; entry balances."""
    await _seeded(db)

    sup = Supplier(name="TestSupplier")
    db.add(sup)
    await db.flush()

    # Diamond product: stone_cost_usd=200
    prod = Product(
        code="DIAM-001",
        name_en="Diamond Ring",
        name_ar="",
        category="Rings",
        karat=Karat.K18,
        weight_grams=D("5.000"),
        margin_percent=D("20"),
        making_charge=D("0"),
        stone_cost_usd=D("200"),
    )
    db.add(prod)
    await db.flush()

    pur = SupplierPurchase(
        supplier_id=sup.id,
        payment_mode=SupplierPurchaseMode.CASH,
        total_cash_due=D("500"),
        cash_paid_at_creation=D("0"),
        total_grams_due_by_karat={},
        grams_paid_at_creation_by_karat={},
        created_by_user_id="u1",
    )
    db.add(pur)
    await db.flush()

    item = SupplierPurchaseItem(
        purchase_id=pur.id,
        item_kind=SupplierItemKind.PRODUCT,
        product_id=prod.id,
        quantity=1,
        unit_cost_usd=D("500"),
    )
    db.add(item)
    await db.flush()

    entry = await glp.post_supplier_purchase(db, pur, _settings(), "u1")
    assert entry is not None

    # Load lines with account system_key
    rows = (
        await db.execute(
            select(GLJournalLine, GLAccount.system_key)
            .join(GLAccount, GLJournalLine.account_id == GLAccount.id)
            .where(GLJournalLine.entry_id == entry.id)
        )
    ).all()

    by_key: dict[str, list] = {}
    for line, key in rows:
        by_key.setdefault(key, []).append(line)

    # Stone inventory must receive exactly the stone_cost_usd amount
    stone_lines = by_key.get("STONE_INVENTORY", [])
    assert stone_lines, "Expected a STONE_INVENTORY debit line"
    stone_debit = sum(l.base_debit for l in stone_lines)
    assert stone_debit == D("200.00"), f"STONE_INVENTORY debit = {stone_debit}"

    # Product inventory gets the remainder
    prod_lines = by_key.get("PRODUCT_INVENTORY", [])
    assert prod_lines, "Expected a PRODUCT_INVENTORY debit line"
    prod_debit = sum(l.base_debit for l in prod_lines)
    assert prod_debit == D("300.00"), f"PRODUCT_INVENTORY debit = {prod_debit}"

    # Entry must balance: total debits == total credits
    total_debit = sum(l.base_debit for l, _ in rows)
    total_credit = sum(l.base_credit for l, _ in rows)
    assert total_debit == total_credit, (
        f"Entry unbalanced: debits={total_debit} credits={total_credit}"
    )


@pytest.mark.asyncio
async def test_non_diamond_product_no_stone_split(db):
    """Regression: product with no stone_cost_usd posts full amount to PRODUCT_INVENTORY."""
    await _seeded(db)

    sup = Supplier(name="TestSupplier2")
    db.add(sup)
    await db.flush()

    # Plain product: no stone_cost_usd
    prod = Product(
        code="PLAIN-001",
        name_en="Plain Bangle",
        name_ar="",
        category="Bangles",
        karat=Karat.K21,
        weight_grams=D("10.000"),
        margin_percent=D("15"),
        making_charge=D("0"),
        # stone_cost_usd intentionally omitted (None)
    )
    db.add(prod)
    await db.flush()

    pur = SupplierPurchase(
        supplier_id=sup.id,
        payment_mode=SupplierPurchaseMode.CASH,
        total_cash_due=D("800"),
        cash_paid_at_creation=D("0"),
        total_grams_due_by_karat={},
        grams_paid_at_creation_by_karat={},
        created_by_user_id="u1",
    )
    db.add(pur)
    await db.flush()

    item = SupplierPurchaseItem(
        purchase_id=pur.id,
        item_kind=SupplierItemKind.PRODUCT,
        product_id=prod.id,
        quantity=1,
        unit_cost_usd=D("800"),
    )
    db.add(item)
    await db.flush()

    entry = await glp.post_supplier_purchase(db, pur, _settings(), "u1")
    assert entry is not None

    rows = (
        await db.execute(
            select(GLJournalLine, GLAccount.system_key)
            .join(GLAccount, GLJournalLine.account_id == GLAccount.id)
            .where(GLJournalLine.entry_id == entry.id)
        )
    ).all()

    by_key: dict[str, list] = {}
    for line, key in rows:
        by_key.setdefault(key, []).append(line)

    # No STONE_INVENTORY line should appear
    assert "STONE_INVENTORY" not in by_key, (
        "STONE_INVENTORY line appeared for non-diamond product"
    )

    # Full amount goes to PRODUCT_INVENTORY
    prod_lines = by_key.get("PRODUCT_INVENTORY", [])
    assert prod_lines, "Expected a PRODUCT_INVENTORY debit line"
    prod_debit = sum(l.base_debit for l in prod_lines)
    assert prod_debit == D("800.00"), f"PRODUCT_INVENTORY debit = {prod_debit}"

    # Entry must balance
    total_debit = sum(l.base_debit for l, _ in rows)
    total_credit = sum(l.base_credit for l, _ in rows)
    assert total_debit == total_credit, (
        f"Entry unbalanced: debits={total_debit} credits={total_credit}"
    )
