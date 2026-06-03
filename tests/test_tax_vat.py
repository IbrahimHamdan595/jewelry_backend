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
