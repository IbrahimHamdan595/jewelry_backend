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


from sqlalchemy import select as _sel
from app.models import VendorBill as _VB


@pytest.mark.asyncio
async def test_vendor_payment_fifo_and_tieout(db):
    await _seed(db)
    b1 = await expenses.post_vendor_bill(db, vendor_name="Landlord", supplier_id=None,
        bill_date=date(2026, 6, 1), due_date=None,
        lines=[{"description": "a", "expense_system_key": "RENT_EXPENSE", "amount": D("100")}],
        payment_system_key=None, memo="", settings=_settings(True), actor_user_id="u1")
    b2 = await expenses.post_vendor_bill(db, vendor_name="Landlord", supplier_id=None,
        bill_date=date(2026, 6, 2), due_date=None,
        lines=[{"description": "b", "expense_system_key": "RENT_EXPENSE", "amount": D("50")}],
        payment_system_key=None, memo="", settings=_settings(True), actor_user_id="u1")
    pay = await expenses.post_vendor_payment(db, vendor_name="Landlord", payment_date=date(2026, 6, 5),
        amount=D("120"), payment_system_key="CASH", memo="", settings=_settings(True), actor_user_id="u1")
    b1r = (await db.execute(_sel(_VB).where(_VB.id == b1.id))).scalar_one()
    b2r = (await db.execute(_sel(_VB).where(_VB.id == b2.id))).scalar_one()
    assert b1r.status == VendorBillStatus.PAID and b2r.amount_paid == D("20.00")
    assert pay.unapplied_amount == D("0.00")
    v = await expenses.verify_vendor_ap(db)
    assert v["gl"] == D("30.00") and v["subledger"] == D("30.00") and v["matches"]
