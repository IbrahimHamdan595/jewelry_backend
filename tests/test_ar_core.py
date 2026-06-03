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
