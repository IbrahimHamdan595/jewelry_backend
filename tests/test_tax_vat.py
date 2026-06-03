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


async def _resolve_id(db, key):
    from app.models import GLAccount
    return (await db.execute(select(GLAccount).where(GLAccount.system_key == key))).scalar_one().id


@pytest.mark.asyncio
async def test_vat_return_nets_output_minus_input(db):
    await _seed(db)
    std = (await db.execute(select(TaxCode).where(TaxCode.code == "STANDARD"))).scalar_one()
    # Input VAT 110 via a vendor bill in June (Q2).
    await expenses.post_vendor_bill(db, vendor_name="L", supplier_id=None, bill_date=date(2026, 6, 3),
        due_date=None, lines=[{"description": "r", "expense_system_key": "RENT_EXPENSE", "amount": D("1000")}],
        payment_system_key=None, memo="", settings=_settings(True), actor_user_id="u1", tax_code_id=std.id)
    # Output VAT 200 via a manual journal CR VAT_PAYABLE / DR Cash (stand-in for a sale).
    cash = await _resolve_id(db, "CASH"); vatp = await _resolve_id(db, "VAT_PAYABLE")
    await gl.post_entry(db, entry_date=date(2026, 6, 10), memo="sale vat", source_type="MANUAL", source_id=None,
        lines=[gl.GLLine(account_id=cash, denomination="MONEY", base_debit=D("200"), money_debit=D("200")),
               gl.GLLine(account_id=vatp, denomination="MONEY", base_credit=D("200"), money_credit=D("200"))],
        actor_user_id="u1")
    ret = await tax.compute_vat_return(db, year=2026, quarter=2)
    assert ret["output_vat"] == D("200.00")
    assert ret["input_vat"] == D("110.00")
    assert ret["net_payable"] == D("90.00") and ret["direction"] == "PAYABLE"
    assert ret["cash_split"]["cash_75"] == D("67.50") and ret["cash_split"]["transfer_25"] == D("22.50")
    q1 = await tax.compute_vat_return(db, year=2026, quarter=1)
    assert q1["output_vat"] == D("0.00") and q1["net_payable"] == D("0.00")
