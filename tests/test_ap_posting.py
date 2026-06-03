from datetime import date
from decimal import Decimal as D

import pytest

from app.core import gl, gl_postings
from app.core.coa_seed import seed_chart_of_accounts
from app.models import (
    GLPeriod, PeriodStatus, Settings, Supplier, SupplierPurchase, SupplierPurchaseItem,
    SupplierPurchaseMode, SupplierItemKind, Karat,
)


async def _seed(db):
    await seed_chart_of_accounts(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    await db.flush()


def _settings(on=True):
    return Settings(id="singleton", accounting_auto_post_enabled=on)


@pytest.mark.asyncio
async def test_cash_purchase_partial_pay_nets_ap(db):
    await _seed(db)
    sup = Supplier(name="ACME"); db.add(sup); await db.flush()
    # Owe 1000 cash, pay 300 now → AP should be 700, Cash -300, Inventory +1000.
    pur = SupplierPurchase(supplier_id=sup.id, payment_mode=SupplierPurchaseMode.CASH,
                           total_cash_due=D("1000"), total_grams_due_by_karat={},
                           cash_paid_at_creation=D("300"), grams_paid_at_creation_by_karat={},
                           created_by_user_id="u1")
    pur.items = [SupplierPurchaseItem(item_kind=SupplierItemKind.PRODUCT, unit_cost_usd=D("1000"))]
    db.add(pur); await db.flush()
    await gl_postings.post_supplier_purchase(db, pur, _settings(), "u1")
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    accts = {a["system_key"]: a for a in tb["accounts"] if a["system_key"]}
    assert tb["balanced"] is True
    assert accts["AP"]["base_credit"] == D("700.00")
    assert accts["CASH"]["base_credit"] == D("300.00")
    assert accts["PRODUCT_INVENTORY"]["base_debit"] == D("1000.00")


@pytest.mark.asyncio
async def test_gold_purchase_partial_pay_nets_metal_ap(db):
    await _seed(db)
    sup = Supplier(name="ACME"); db.add(sup); await db.flush()
    # Owe 50g K21, pay 20g now → Metal AP net 30g.
    pur = SupplierPurchase(supplier_id=sup.id, payment_mode=SupplierPurchaseMode.GOLD,
                           total_cash_due=D("0"), total_grams_due_by_karat={"K21": "50.000"},
                           cash_paid_at_creation=D("0"), grams_paid_at_creation_by_karat={"K21": "20.000"},
                           created_by_user_id="u1")
    pur.items = [SupplierPurchaseItem(item_kind=SupplierItemKind.PURE_GOLD, karat=Karat.K21,
                                      weight_grams=D("50.000"), unit_cost_usd=D("3000"))]
    db.add(pur); await db.flush()
    await gl_postings.post_supplier_purchase(db, pur, _settings(), "u1")
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    accts = {a["system_key"]: a for a in tb["accounts"] if a["system_key"]}
    assert tb["metal_balanced"] is True
    assert accts["METAL_AP"]["metal_by_karat"]["K21"]["net_grams"] == D("-30.000")  # credit 30
    assert accts["METAL_INVENTORY"]["metal_by_karat"]["K21"]["net_grams"] == D("30.000")
