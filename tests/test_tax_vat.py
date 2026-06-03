import pytest
from sqlalchemy import select

from app.core import tax
from app.models import TaxCode, VendorBill


def test_taxcode_model_importable():
    assert TaxCode.__tablename__ == "tax_codes"
    assert hasattr(VendorBill, "tax_code_id") and hasattr(VendorBill, "subtotal") and hasattr(VendorBill, "vat_amount")


@pytest.mark.asyncio
async def test_seed_tax_codes_idempotent(db):
    n = await tax.seed_tax_codes(db)
    assert n == 3
    again = await tax.seed_tax_codes(db)
    assert again == 0
    std = (await db.execute(select(TaxCode).where(TaxCode.code == "STANDARD"))).scalar_one()
    assert std.rate == __import__("decimal").Decimal("11.00")


from datetime import date
from decimal import Decimal as D
from app.core import gl, expenses
from app.core.coa_seed import seed_chart_of_accounts
from app.models import GLPeriod, PeriodStatus, Settings, VendorBillStatus


async def _seed(db):
    await seed_chart_of_accounts(db)
    await tax.seed_tax_codes(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    await db.flush()


def _settings(on=True):
    return Settings(id="singleton", accounting_auto_post_enabled=on)


@pytest.mark.asyncio
async def test_vendor_bill_with_standard_vat_splits_input_vat(db):
    await _seed(db)
    std = (await db.execute(select(TaxCode).where(TaxCode.code == "STANDARD"))).scalar_one()
    bill = await expenses.post_vendor_bill(
        db, vendor_name="Landlord", supplier_id=None, bill_date=date(2026, 6, 3), due_date=None,
        lines=[{"description": "rent", "expense_system_key": "RENT_EXPENSE", "amount": D("1000")}],
        payment_system_key=None, memo="", settings=_settings(True), actor_user_id="u1", tax_code_id=std.id)
    assert bill.subtotal == D("1000.00") and bill.vat_amount == D("110.00") and bill.total == D("1110.00")
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    accts = {a["system_key"]: a for a in tb["accounts"] if a["system_key"]}
    assert accts["RENT_EXPENSE"]["base_debit"] == D("1000.00")
    assert accts["VAT_RECEIVABLE"]["base_debit"] == D("110.00")
    assert accts["VENDOR_AP"]["base_credit"] == D("1110.00")


@pytest.mark.asyncio
async def test_vendor_bill_no_tax_code_unchanged(db):
    await _seed(db)
    bill = await expenses.post_vendor_bill(
        db, vendor_name="Cafe", supplier_id=None, bill_date=date(2026, 6, 3), due_date=None,
        lines=[{"description": "x", "expense_system_key": "OFFICE_EXPENSE", "amount": D("40")}],
        payment_system_key="CASH", memo="", settings=_settings(True), actor_user_id="u1")
    assert bill.vat_amount == D("0.00") and bill.total == D("40.00") and bill.subtotal == D("40.00")
