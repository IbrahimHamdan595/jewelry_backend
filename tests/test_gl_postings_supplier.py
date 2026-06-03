from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core import gl
from app.core import gl_postings as glp
from app.core.coa_seed import seed_chart_of_accounts
from app.models import (
    GLPeriod, PeriodStatus, Settings, Supplier, SupplierPurchase, SupplierPurchaseItem,
    SupplierPurchaseMode, SupplierItemKind, SupplierPayment, DebtUnit, Karat,
)

D = Decimal


async def _seeded(db):
    await seed_chart_of_accounts(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    await db.flush()


def _settings(on=True):
    return Settings(id="singleton", accounting_auto_post_enabled=on)


@pytest.mark.asyncio
async def test_supplier_gold_purchase_balances(db):
    await _seeded(db)
    sup = Supplier(name="ACME"); db.add(sup); await db.flush()
    pur = SupplierPurchase(
        supplier_id=sup.id, payment_mode=SupplierPurchaseMode.GOLD,
        total_cash_due=D("0"), total_grams_due_by_karat={"K21": "50.000"},
        cash_paid_at_creation=D("0"), grams_paid_at_creation_by_karat={},
        created_by_user_id="u1",
    )
    pur.items = [SupplierPurchaseItem(item_kind=SupplierItemKind.PURE_GOLD, karat=Karat.K21,
                                      weight_grams=D("50.000"), unit_cost_usd=D("3000"))]
    db.add(pur); await db.flush()
    entry = await glp.post_supplier_purchase(db, pur, _settings(), "u1")
    assert entry is not None
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    assert tb["balanced"] and tb["metal_balanced"]
    accts = {a["system_key"]: a for a in tb["accounts"]}
    assert accts["METAL_INVENTORY"]["metal_by_karat"]["K21"]["net_grams"] == D("50.000")
    assert accts["METAL_AP"]["metal_by_karat"]["K21"]["net_grams"] == D("-50.000")  # credit


@pytest.mark.asyncio
async def test_supplier_cash_payment_balances(db):
    await _seeded(db)
    sup = Supplier(name="ACME"); db.add(sup); await db.flush()
    pay = SupplierPayment(supplier_id=sup.id, unit=DebtUnit.CASH, karat=None,
                          amount=D("2000"), paid_by_user_id="u1")
    db.add(pay); await db.flush()
    entry = await glp.post_supplier_payment(db, pay, _settings(), "u1")
    assert entry is not None
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    accts = {a["system_key"]: a for a in tb["accounts"]}
    assert accts["AP"]["base_debit"] == D("2000.00")
    assert accts["CASH"]["base_credit"] == D("2000.00")


@pytest.mark.asyncio
async def test_supplier_gold_payment_balances_metal(db):
    await _seeded(db)
    sup = Supplier(name="ACME"); db.add(sup); await db.flush()
    pay = SupplierPayment(supplier_id=sup.id, unit=DebtUnit.GOLD, karat=Karat.K21,
                          amount=D("30.000"), paid_by_user_id="u1")
    db.add(pay); await db.flush()
    entry = await glp.post_supplier_payment(db, pay, _settings(), "u1")
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    assert tb["metal_balanced"]
    accts = {a["system_key"]: a for a in tb["accounts"]}
    assert accts["METAL_AP"]["metal_by_karat"]["K21"]["net_grams"] == D("30.000")  # debit (pay down)
    assert accts["METAL_INVENTORY"]["metal_by_karat"]["K21"]["net_grams"] == D("-30.000")
