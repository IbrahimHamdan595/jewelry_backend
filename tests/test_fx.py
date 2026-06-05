from datetime import date
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.core import ar, expenses, gl
from app.core.coa_seed import seed_chart_of_accounts
from app.core.gl_fx import Allocation, settlement_legs
from app.models import Customer, GLAccount, GLPeriod, PeriodStatus, Settings


def _by_key(legs):
    return {l.system_key: l for l in legs}


def test_settlement_legs_lbp_invoice_moved_rate_loss():
    # LBP invoice booked @89500 ($1000), paid in full LBP cash when rate is 92000.
    legs = settlement_legs(
        kind="receipt", recorded_currency="LBP",
        allocations=[Allocation("LBP", D("89500"), D("89500000"))],
        control_system_key="AR", cash_system_key="CASH_LBP",
        tender_currency="LBP", tender_fx_rate=D("92000"),
    )
    k = _by_key(legs)
    assert k["AR"].base == D("1000.00") and not k["AR"].debit
    assert k["CASH_LBP"].money == D("89500000.00") and k["CASH_LBP"].base == D("972.83") and k["CASH_LBP"].debit
    assert k["FX_LOSS"].debit and k["FX_LOSS"].base == D("27.17")
    # base balances
    dr = sum(l.base for l in legs if l.debit)
    cr = sum(l.base for l in legs if not l.debit)
    assert dr == cr == D("1000.00")


def test_settlement_legs_gain():
    # Same invoice, but LBP strengthened (rate 87000) → realized gain.
    legs = settlement_legs(
        kind="receipt", recorded_currency="LBP",
        allocations=[Allocation("LBP", D("89500"), D("89500000"))],
        control_system_key="AR", cash_system_key="CASH_LBP",
        tender_currency="LBP", tender_fx_rate=D("87000"),
    )
    k = _by_key(legs)
    assert "FX_LOSS" not in k
    assert k["FX_GAIN"].debit is False and k["FX_GAIN"].base == D("28.74")  # 89.5M/87000=1028.74 − 1000
    dr = sum(l.base for l in legs if l.debit)
    cr = sum(l.base for l in legs if not l.debit)
    assert dr == cr


def test_settlement_usd_invoice_lbp_cash_no_fx():
    # USD invoice paid with LBP cash, recorded in USD → no invoice-level FX.
    legs = settlement_legs(
        kind="receipt", recorded_currency="USD",
        allocations=[Allocation("USD", D("1"), D("1000"))],
        control_system_key="AR", cash_system_key="CASH_LBP",
        tender_currency="LBP", tender_fx_rate=D("92000"),
    )
    k = _by_key(legs)
    assert "FX_LOSS" not in k and "FX_GAIN" not in k
    assert k["AR"].base == D("1000.00") and not k["AR"].debit
    assert k["CASH_LBP"].money == D("92000000.00") and k["CASH_LBP"].base == D("1000.00") and k["CASH_LBP"].debit


def test_settlement_rejects_cross_currency():
    with pytest.raises(ValueError):
        settlement_legs(
            kind="receipt", recorded_currency="USD",
            allocations=[Allocation("LBP", D("89500"), D("89500000"))],
            control_system_key="AR", cash_system_key="CASH",
            tender_currency="USD", tender_fx_rate=D("1"),
        )


def test_payment_loss_and_gain_sign_mirror():
    # AP: bill LBP @89500 ($1000) paid LBP @92000 → you paid less USD → GAIN.
    legs = settlement_legs(
        kind="payment", recorded_currency="LBP",
        allocations=[Allocation("LBP", D("89500"), D("89500000"))],
        control_system_key="VENDOR_AP", cash_system_key="CASH_LBP",
        tender_currency="LBP", tender_fx_rate=D("92000"),
    )
    k = _by_key(legs)
    assert k["VENDOR_AP"].debit and k["VENDOR_AP"].base == D("1000.00")
    assert k["CASH_LBP"].base == D("972.83") and not k["CASH_LBP"].debit
    assert k["FX_GAIN"].base == D("27.17") and not k["FX_GAIN"].debit
    dr = sum(l.base for l in legs if l.debit)
    cr = sum(l.base for l in legs if not l.debit)
    assert dr == cr == D("1000.00")


# ── DB integration (the Definition-of-Done test) ──────────────────────────────

async def _seed(db):
    await seed_chart_of_accounts(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    await db.flush()


def _settings():
    return Settings(id="singleton", accounting_auto_post_enabled=True, lbp_exchange_rate=D("92000"))


async def _acct_base(db, system_key):
    a = (await db.execute(select(GLAccount).where(GLAccount.system_key == system_key))).scalar_one()
    from app.models import GLJournalLine
    lines = (await db.execute(select(GLJournalLine).where(GLJournalLine.account_id == a.id))).scalars().all()
    return (sum((l.base_debit for l in lines), D("0")), sum((l.base_credit for l in lines), D("0")))


async def _acct_line_count(db, system_key):
    """How many GL lines touch this account — 0 proves no leg was emitted at all
    (a stray $0.00 leg would still count, so this is stricter than a net-zero check)."""
    a = (await db.execute(select(GLAccount).where(GLAccount.system_key == system_key))).scalar_one()
    from app.models import GLJournalLine
    return len((await db.execute(select(GLJournalLine).where(GLJournalLine.account_id == a.id))).scalars().all())


@pytest.mark.asyncio
async def test_lbp_invoice_receipt_at_moved_rate_books_fx_loss(db):
    await _seed(db)
    cust = Customer(name="LBP Trader", currency="LBP")
    db.add(cust)
    await db.flush()
    s = _settings()
    # LBP invoice: 89,500,000 LBP @ 89500 → base $1000.00
    inv = await ar.post_standalone_invoice(
        db, customer_id=cust.id, invoice_date=date(2026, 6, 5), due_date=None,
        lines=[{"description": "goods", "unit_price": "89500000", "quantity": 1}],
        memo="lbp", vat_percent=D("0"), settings=s, actor_user_id="u1", fx_rate=D("89500"))
    assert inv.fx_rate == D("89500") and inv.total == D("89500000.00")

    # Settle in full with LBP cash when the rate has moved to 92000.
    rc = await ar.post_receipt(
        db, customer_id=cust.id, receipt_date=date(2026, 6, 6), amount=D("89500000"),
        payment_system_key="CASH_LBP", memo="pay", settings=s, actor_user_id="u1",
        currency="LBP", fx_rate=D("92000"))
    assert rc.gl_entry_id is not None  # (a) posts

    # (b) trial balance balanced in base AND per karat
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    assert tb["balanced"] is True and tb["metal_balanced"] is True

    # (c) realized loss in FX_LOSS with the right sign/amount: $1000 − $972.83
    dr, cr = await _acct_base(db, "FX_LOSS")
    assert (dr - cr) == D("27.17")
    # a loss must NOT also touch the gain account — assert absence, not net-zero
    assert await _acct_line_count(db, "FX_GAIN") == 0

    # (d) AR control ties out in base (fully settled → 0)
    v = await ar.verify_ar_control(db)
    assert v["matches"] is True and v["gl_ar_balance"] == D("0.00")


@pytest.mark.asyncio
async def test_ap_lbp_bill_payment_at_moved_rate_books_fx_gain(db):
    await _seed(db)
    s = _settings()
    # LBP bill 89,500,000 @ 89500 → base $1000, on credit (VENDOR_AP)
    bill = await expenses.post_vendor_bill(
        db, vendor_name="LBP Supplier", supplier_id=None, bill_date=date(2026, 6, 5), due_date=None,
        lines=[{"description": "rent", "expense_system_key": "RENT_EXPENSE", "amount": "89500000"}],
        payment_system_key=None, memo="lbp", settings=s, actor_user_id="u1",
        currency="LBP", fx_rate=D("89500"))
    assert bill.fx_rate == D("89500")

    # Pay in full with LBP cash when LBP weakened to 92000 → 89.5M LBP is worth
    # only $972.83, so a $1000 bill is settled for less USD → realized GAIN.
    pay = await expenses.post_vendor_payment(
        db, vendor_name="LBP Supplier", payment_date=date(2026, 6, 6), amount=D("89500000"),
        payment_system_key="CASH_LBP", memo="pay", settings=s, actor_user_id="u1",
        currency="LBP", fx_rate=D("92000"))
    assert pay.gl_entry_id is not None

    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    assert tb["balanced"] is True and tb["metal_balanced"] is True

    gdr, gcr = await _acct_base(db, "FX_GAIN")
    assert (gcr - gdr) == D("27.17")  # 1000 − 89.5M/92000(972.83)
    assert await _acct_line_count(db, "FX_LOSS") == 0
    v = await expenses.verify_vendor_ap(db)
    assert v["matches"] is True and v["gl"] == D("0.00")


@pytest.mark.asyncio
async def test_usd_invoice_lbp_cash_emits_no_fx_line(db):
    """USD invoice settled with LBP cash, recorded in USD: there is no invoice-level
    FX exposure, so NO FX leg may be posted — not a $0.00 one. Asserting the line
    count is 0 (not the net) is what catches a stray zero-value FX leg."""
    await _seed(db)
    cust = Customer(name="USD Buyer", currency="USD")
    db.add(cust)
    await db.flush()
    s = _settings()
    inv = await ar.post_standalone_invoice(
        db, customer_id=cust.id, invoice_date=date(2026, 6, 5), due_date=None,
        lines=[{"description": "goods", "unit_price": "1000", "quantity": 1}],
        memo="usd", vat_percent=D("0"), settings=s, actor_user_id="u1", fx_rate=D("1"))
    assert inv.fx_rate == D("1") and inv.total == D("1000.00")

    # Pay the USD invoice with LBP cash at today's rate 92000 (tender = CASH_LBP).
    rc = await ar.post_receipt(
        db, customer_id=cust.id, receipt_date=date(2026, 6, 6), amount=D("1000"),
        payment_system_key="CASH_LBP", memo="lbp cash", settings=s, actor_user_id="u1",
        currency="USD", fx_rate=D("92000"))
    assert rc.gl_entry_id is not None

    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    assert tb["balanced"] is True and tb["metal_balanced"] is True

    # No FX leg at all — neither account is touched.
    assert await _acct_line_count(db, "FX_GAIN") == 0
    assert await _acct_line_count(db, "FX_LOSS") == 0
    # Cash recorded in LBP at the tender rate; base is the USD face value.
    cdr, _ = await _acct_base(db, "CASH_LBP")
    assert cdr == D("1000.00")
    v = await ar.verify_ar_control(db)
    assert v["matches"] is True and v["gl_ar_balance"] == D("0.00")


@pytest.mark.asyncio
async def test_lbp_invoice_awkward_rate_residual_balances_to_cent(db):
    """Non-round rates (89537 → 91123): the quantized residual must land entirely in
    the FX line and the trial balance must still net to zero to the cent."""
    await _seed(db)
    cust = Customer(name="Awkward LBP", currency="LBP")
    db.add(cust)
    await db.flush()
    s = _settings()
    # 89,500,000 LBP @ 89537 → base $999.59
    inv = await ar.post_standalone_invoice(
        db, customer_id=cust.id, invoice_date=date(2026, 6, 5), due_date=None,
        lines=[{"description": "goods", "unit_price": "89500000", "quantity": 1}],
        memo="awkward", vat_percent=D("0"), settings=s, actor_user_id="u1", fx_rate=D("89537"))
    assert inv.fx_rate == D("89537")

    # Settle in full with LBP cash at 91123 → cash base $982.19, residual $17.40 loss.
    rc = await ar.post_receipt(
        db, customer_id=cust.id, receipt_date=date(2026, 6, 6), amount=D("89500000"),
        payment_system_key="CASH_LBP", memo="pay", settings=s, actor_user_id="u1",
        currency="LBP", fx_rate=D("91123"))
    assert rc.gl_entry_id is not None

    # Trial balance nets to zero to the cent despite the awkward rates.
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    assert tb["balanced"] is True and tb["metal_balanced"] is True

    # The full quantized residual landed in exactly one FX_LOSS line.
    assert await _acct_line_count(db, "FX_LOSS") == 1
    dr, cr = await _acct_base(db, "FX_LOSS")
    assert (dr - cr) == D("17.40")
    assert await _acct_line_count(db, "FX_GAIN") == 0
    v = await ar.verify_ar_control(db)
    assert v["matches"] is True and v["gl_ar_balance"] == D("0.00")
