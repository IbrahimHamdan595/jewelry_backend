import pytest
from app.models import (
    Customer, ARInvoice, ARInvoiceLine, ARReceipt, ARReceiptAllocation,
    ARInvoiceStatus, PaymentMethod,
)


def test_ar_enums():
    assert {s.value for s in ARInvoiceStatus} == {"OPEN", "PARTIAL", "PAID", "VOID"}
    assert PaymentMethod.CREDIT.value == "CREDIT"


@pytest.mark.asyncio
async def test_ar_models_create(db):
    from decimal import Decimal as D
    from datetime import date
    c = Customer(name="Acme Co", currency="USD", credit_limit=D("1000"))
    db.add(c)
    await db.flush()
    inv = ARInvoice(invoice_no="AR-20260603-001", customer_id=c.id, invoice_date=date(2026, 6, 3),
                    currency="USD", subtotal=D("100"), vat_amount=D("11"), total=D("111"),
                    status=ARInvoiceStatus.OPEN)
    db.add(inv)
    await db.flush()
    assert inv.amount_paid == D("0")


from datetime import date
from decimal import Decimal as D
from sqlalchemy import select
from app.core import ar


@pytest.mark.asyncio
async def test_next_doc_no_per_prefix(db):
    a1 = await ar._next_doc_no(db, "AR", date(2026, 6, 3))
    a2 = await ar._next_doc_no(db, "AR", date(2026, 6, 3))
    r1 = await ar._next_doc_no(db, "RC", date(2026, 6, 3))
    assert a1 == "AR-20260603-001"
    assert a2 == "AR-20260603-002"
    assert r1 == "RC-20260603-001"  # separate sequence


@pytest.mark.asyncio
async def test_open_balance_and_credit_limit(db):
    c = Customer(name="C", currency="USD", credit_limit=D("150"))
    db.add(c)
    await db.flush()
    db.add(ARInvoice(invoice_no="AR-1", customer_id=c.id, invoice_date=date(2026, 6, 1),
                     subtotal=D("100"), vat_amount=D("0"), total=D("100"), status=ARInvoiceStatus.OPEN))
    await db.flush()
    bal = await ar.customer_open_balance(db, c.id)
    assert bal == D("100.00")
    await ar.check_credit_limit(db, c, D("40"))
    with pytest.raises(Exception):
        await ar.check_credit_limit(db, c, D("60"))
