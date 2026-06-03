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


from app.core import gl, gl_postings
from app.core.coa_seed import seed_chart_of_accounts
from app.models import GLPeriod, PeriodStatus, Order, OrderItem, OrderItemKind, Karat, Settings


def _settings(on=True):
    return Settings(id="singleton", accounting_auto_post_enabled=on, vat_percent=D("11"))


@pytest.mark.asyncio
async def test_credit_order_debits_ar_and_creates_invoice(db):
    await seed_chart_of_accounts(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    cust = Customer(name="Acme", currency="USD")
    db.add(cust)
    await db.flush()
    order = Order(order_number="ORD-9", cashier_id="u1", payment_method=PaymentMethod.CREDIT,
                  customer_id=cust.id, subtotal=D("100"), vat_percent=D("11"), vat_amount=D("11"),
                  discount_percent=D("0"), discount_amount=D("0"), total_usd=D("111"),
                  total_lbp=D("0"), lbp_exchange_rate=D("89500"))
    order.items = [OrderItem(item_kind=OrderItemKind.COIN, product_code="C", product_name="Coin",
                             karat=Karat.K21, weight_grams=D("10"), gold_rate_at_sale=D("60"),
                             margin_percent=D("0"), making_charge=D("0"), final_price=D("100"), quantity=1)]
    db.add(order)
    await db.flush()
    entry = await gl_postings.post_sale(db, order, _settings(on=True), "u1")
    inv = await ar.create_invoice_from_order(db, order=order, customer_id=cust.id, gl_entry=entry, actor_user_id="u1")
    assert inv.total == D("111.00") and inv.order_id == order.id
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    accts = {a["system_key"]: a for a in tb["accounts"] if a["system_key"]}
    assert accts["AR"]["base_debit"] == D("111.00")
    assert "CASH" not in accts or accts["CASH"]["base_debit"] == D("0.00")
    v = await ar.verify_ar_control(db)
    assert v["matches"] is True
