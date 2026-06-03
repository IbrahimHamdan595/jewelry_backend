from datetime import date
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.core import gl, ar
from app.core.coa_seed import seed_chart_of_accounts
from app.models import (
    GLPeriod, PeriodStatus, Settings, Customer, ARInvoice, ARInvoiceStatus, ARReceipt,
)


async def _seed(db):
    await seed_chart_of_accounts(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    db.add(GLPeriod(year=2026, period_no=4, status=PeriodStatus.OPEN))
    await db.flush()


def _settings(on=True):
    return Settings(id="singleton", accounting_auto_post_enabled=on, vat_percent=D("11"))


@pytest.mark.asyncio
async def test_standalone_invoice_posts_ar(db):
    await _seed(db)
    c = Customer(name="C", currency="USD"); db.add(c); await db.flush()
    inv = await ar.post_standalone_invoice(
        db, customer_id=c.id, invoice_date=date(2026, 6, 3), due_date=None,
        lines=[{"description": "repair", "quantity": 1, "unit_price": D("200")}],
        memo="svc", vat_percent=D("11"), settings=_settings(True), actor_user_id="u1")
    assert inv.total == D("222.00")
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    accts = {a["system_key"]: a for a in tb["accounts"] if a["system_key"]}
    assert accts["AR"]["base_debit"] == D("222.00")
    assert accts["SALES_REVENUE"]["base_credit"] == D("200.00")


@pytest.mark.asyncio
async def test_receipt_fifo_partial_and_overpayment(db):
    await _seed(db)
    c = Customer(name="C", currency="USD"); db.add(c); await db.flush()
    i1 = await ar.post_standalone_invoice(db, customer_id=c.id, invoice_date=date(2026, 6, 1), due_date=None,
        lines=[{"description": "a", "quantity": 1, "unit_price": D("100")}], memo="", vat_percent=D("0"),
        settings=_settings(True), actor_user_id="u1")
    i2 = await ar.post_standalone_invoice(db, customer_id=c.id, invoice_date=date(2026, 6, 2), due_date=None,
        lines=[{"description": "b", "quantity": 1, "unit_price": D("50")}], memo="", vat_percent=D("0"),
        settings=_settings(True), actor_user_id="u1")
    r = await ar.post_receipt(db, customer_id=c.id, receipt_date=date(2026, 6, 5), amount=D("120"),
                              payment_system_key="CASH", memo="", settings=_settings(True), actor_user_id="u1")
    i1r = (await db.execute(select(ARInvoice).where(ARInvoice.id == i1.id))).scalar_one()
    i2r = (await db.execute(select(ARInvoice).where(ARInvoice.id == i2.id))).scalar_one()
    assert i1r.status == ARInvoiceStatus.PAID and i1r.amount_paid == D("100.00")
    assert i2r.status == ARInvoiceStatus.PARTIAL and i2r.amount_paid == D("20.00")
    assert r.unapplied_amount == D("0.00")
    r2 = await ar.post_receipt(db, customer_id=c.id, receipt_date=date(2026, 6, 6), amount=D("100"),
                               payment_system_key="CASH", memo="", settings=_settings(True), actor_user_id="u1")
    assert r2.unapplied_amount == D("70.00")
    bal = await ar.customer_open_balance(db, c.id)
    assert bal == D("-70.00")  # they have a 70 credit


@pytest.mark.asyncio
async def test_aging_buckets(db):
    await _seed(db)
    c = Customer(name="C", currency="USD"); db.add(c); await db.flush()
    # Invoice 40 days old (→ 31-60 bucket) for 100.
    await ar.post_standalone_invoice(db, customer_id=c.id, invoice_date=date(2026, 4, 26), due_date=None,
        lines=[{"description": "a", "quantity": 1, "unit_price": D("100")}], memo="", vat_percent=D("0"),
        settings=_settings(True), actor_user_id="u1")
    aging = await ar.compute_aging(db, as_of=date(2026, 6, 5))
    assert aging["totals"]["31_60"] == D("100.00")
    assert aging["totals"]["0_30"] == D("0.00")
