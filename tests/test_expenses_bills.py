import pytest
from sqlalchemy import select

from app.core.coa_seed import seed_chart_of_accounts
from app.models import (
    VendorBill, VendorBillLine, VendorPayment, VendorPaymentAllocation,
    VendorBillStatus, GLAccount,
)


def test_vendor_bill_status_enum():
    assert {s.value for s in VendorBillStatus} == {"OPEN", "PARTIAL", "PAID", "VOID"}


@pytest.mark.asyncio
async def test_expense_accounts_seeded(db):
    await seed_chart_of_accounts(db)
    keys = {a.system_key for a in (await db.execute(select(GLAccount))).scalars().all()}
    for k in ("VENDOR_AP", "RENT_EXPENSE", "UTILITIES_EXPENSE", "SALARIES_EXPENSE",
              "MARKETING_EXPENSE", "BANK_CHARGES_EXPENSE", "OFFICE_EXPENSE", "MISC_EXPENSE"):
        assert k in keys


from datetime import date
from decimal import Decimal as D
from app.core import gl, expenses
from app.models import GLPeriod, PeriodStatus, Settings


async def _seed(db):
    await seed_chart_of_accounts(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    await db.flush()


def _settings(on=True):
    return Settings(id="singleton", accounting_auto_post_enabled=on)


@pytest.mark.asyncio
async def test_bill_on_credit_posts_vendor_ap(db):
    await _seed(db)
    bill = await expenses.post_vendor_bill(
        db, vendor_name="Landlord", supplier_id=None, bill_date=date(2026, 6, 3), due_date=None,
        lines=[{"description": "June rent", "expense_system_key": "RENT_EXPENSE", "amount": D("1500")}],
        payment_system_key=None, memo="rent", settings=_settings(True), actor_user_id="u1")
    assert bill.total == D("1500.00") and bill.status == VendorBillStatus.OPEN
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    accts = {a["system_key"]: a for a in tb["accounts"] if a["system_key"]}
    assert accts["RENT_EXPENSE"]["base_debit"] == D("1500.00")
    assert accts["VENDOR_AP"]["base_credit"] == D("1500.00")


@pytest.mark.asyncio
async def test_bill_paid_now_credits_cash(db):
    await _seed(db)
    bill = await expenses.post_vendor_bill(
        db, vendor_name="Cafe", supplier_id=None, bill_date=date(2026, 6, 3), due_date=None,
        lines=[{"description": "snacks", "expense_system_key": "OFFICE_EXPENSE", "amount": D("40")}],
        payment_system_key="CASH", memo="", settings=_settings(True), actor_user_id="u1")
    assert bill.status == VendorBillStatus.PAID and bill.amount_paid == D("40.00")
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    accts = {a["system_key"]: a for a in tb["accounts"] if a["system_key"]}
    assert accts["OFFICE_EXPENSE"]["base_debit"] == D("40.00")
    assert accts["CASH"]["base_credit"] == D("40.00")
