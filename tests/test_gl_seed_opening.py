from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core import gl
from app.core.coa_seed import seed_chart_of_accounts, post_opening_balances, SYSTEM_ACCOUNTS
from app.models import (
    GLAccount, GLPeriod, PeriodStatus, Denomination, GoldLot, LotSource, Karat,
    Supplier, SupplierBalance, DebtUnit,
)

D = Decimal


@pytest.mark.asyncio
async def test_seed_creates_all_system_accounts_idempotently(db):
    created = await seed_chart_of_accounts(db)
    assert created == len(SYSTEM_ACCOUNTS)

    keys = {a.system_key for a in (await db.execute(select(GLAccount))).scalars().all()}
    for required in ("CASH", "BANK", "METAL_INVENTORY", "AP", "METAL_AP",
                     "VAT_PAYABLE", "OPENING_BALANCE_EQUITY", "RETAINED_EARNINGS",
                     "SALES_REVENUE", "METAL_COGS", "FX_GAIN_LOSS"):
        assert required in keys

    # DUAL accounts carry grams + money.
    inv = (await db.execute(select(GLAccount).where(GLAccount.system_key == "METAL_INVENTORY"))).scalar_one()
    assert inv.denomination == Denomination.DUAL

    # Idempotent: second run creates nothing.
    created_again = await seed_chart_of_accounts(db)
    assert created_again == 0


@pytest.mark.asyncio
async def test_opening_balances_balances_with_equity(db):
    await seed_chart_of_accounts(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    # A gold lot on hand: 100g K21 costing $5000.
    db.add(GoldLot(karat=Karat.K21, weight_grams=D("100"), weight_remaining_grams=D("100"),
                   source=LotSource.SEED, cost_basis_usd=D("5000")))
    # A supplier we owe: $2000 cash + 30g K21.
    sup = Supplier(name="ACME Gold")
    db.add(sup)
    await db.flush()
    db.add(SupplierBalance(supplier_id=sup.id, unit=DebtUnit.CASH, karat="", balance=D("2000")))
    db.add(SupplierBalance(supplier_id=sup.id, unit=DebtUnit.GOLD, karat="K21", balance=D("30")))
    await db.flush()

    await post_opening_balances(
        db, as_of=date(2026, 6, 1), actor_user_id="u1",
        cash_lines=[{"system_key": "CASH", "amount": D("1500")}],
    )
    # Entry must balance in both dimensions (post_entry would have raised otherwise).
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 1))
    assert tb["balanced"] is True
    assert tb["metal_balanced"] is True
    # Opening equity is the balancing plug.
    eq = next(a for a in tb["accounts"] if a["system_key"] == "OPENING_BALANCE_EQUITY")
    assert eq["net_base"] != D("0")
