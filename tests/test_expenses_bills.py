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
