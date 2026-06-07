import pytest
from sqlalchemy import select

from app.core.coa_seed import seed_chart_of_accounts
from app.models import GLAccount, Denomination, Settings


@pytest.mark.asyncio
async def test_new_system_accounts_seeded(db):
    await seed_chart_of_accounts(db)
    keys = {a.system_key for a in (await db.execute(select(GLAccount))).scalars().all()}
    assert "METAL_CLEARING" in keys
    assert "ADJUSTMENT_EXPENSE" in keys
    clearing = (await db.execute(select(GLAccount).where(GLAccount.system_key == "METAL_CLEARING"))).scalar_one()
    assert clearing.denomination == Denomination.DUAL


@pytest.mark.asyncio
async def test_settings_auto_post_flag_defaults_false(db):
    # Python-side default applies on flush, not on transient construction.
    s = Settings(id="singleton")
    db.add(s)
    await db.flush()
    assert s.accounting_auto_post_enabled is False


from datetime import date

from app.core import gl_postings as glp
from app.models import GLPeriod, PeriodStatus


@pytest.mark.asyncio
async def test_ensure_period_creates_open_then_reuses(db):
    p1 = await glp.ensure_period(db, date(2026, 6, 10))
    assert p1.status == PeriodStatus.OPEN and p1.year == 2026 and p1.period_no == 6
    p2 = await glp.ensure_period(db, date(2026, 6, 20))
    assert p2.id == p1.id  # same month reused


@pytest.mark.asyncio
async def test_resolve_account_id(db):
    await seed_chart_of_accounts(db)
    aid = await glp.resolve_account_id(db, "CASH")
    assert aid
    with pytest.raises(Exception):
        await glp.resolve_account_id(db, "NOPE")


def test_auto_post_enabled_reads_flag():
    assert glp.auto_post_enabled(Settings(id="singleton")) is False
    assert glp.auto_post_enabled(Settings(id="singleton", accounting_auto_post_enabled=True)) is True


from decimal import Decimal
from app.core import gl
from app.core.gl_postings import post_sale as gl_postings_post_sale
from app.models import (
    Order, OrderItem, OrderItemKind, PaymentMethod, Karat, GLJournalLine,
)

D = Decimal


async def _seeded(db):
    await seed_chart_of_accounts(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    await db.flush()


def _settings(on=True):
    return Settings(id="singleton", accounting_auto_post_enabled=on, vat_percent=D("11"),
                    lbp_exchange_rate=D("89500"))


async def _make_order(db, *, payment="CASH"):
    order = Order(
        order_number="ORD-1", cashier_id="u1", payment_method=PaymentMethod(payment),
        subtotal=D("100"), vat_percent=D("11"), vat_amount=D("11"),
        discount_percent=D("0"), discount_amount=D("0"),
        total_usd=D("111"), total_lbp=D("9934500"), lbp_exchange_rate=D("89500"),
    )
    order.items = [OrderItem(
        item_kind=OrderItemKind.COIN, product_code="C1", product_name="Coin", karat=Karat.K21,
        weight_grams=D("10.000"), gold_rate_at_sale=D("60.00"), margin_percent=D("0"),
        making_charge=D("0"), final_price=D("100"), quantity=1,
    )]
    db.add(order)
    await db.flush()
    return order


@pytest.mark.asyncio
async def test_post_sale_flag_off_is_noop(db):
    await _seeded(db)
    order = await _make_order(db)
    entry = await gl_postings_post_sale(db, order, _settings(on=False), "u1")
    assert entry is None
    assert (await db.execute(select(GLJournalLine))).scalars().first() is None


@pytest.mark.asyncio
async def test_post_sale_posts_balanced_entry_with_cogs(db):
    await _seeded(db)
    order = await _make_order(db)
    entry = await gl_postings_post_sale(db, order, _settings(on=True), "u1")
    assert entry is not None and entry.source_type == "ORDER" and entry.source_id == order.id
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    assert tb["balanced"] is True and tb["metal_balanced"] is True
    accts = {a["system_key"]: a for a in tb["accounts"]}
    assert accts["CASH"]["base_debit"] == D("111.00")
    assert accts["SALES_REVENUE"]["base_credit"] == D("100.00")
    assert accts["VAT_PAYABLE"]["base_credit"] == D("11.00")
    assert accts["METAL_COGS"]["metal_by_karat"]["K21"]["net_grams"] == D("10.000")
    # COGS cost proxy = 60 * 10 * purity(K21=0.875) = 525.00
    assert accts["METAL_COGS"]["base_debit"] == D("525.00")


@pytest.mark.asyncio
async def test_post_sale_idempotent(db):
    await _seeded(db)
    order = await _make_order(db)
    await gl_postings_post_sale(db, order, _settings(on=True), "u1")
    again = await gl_postings_post_sale(db, order, _settings(on=True), "u1")
    assert again is None
    from app.models import GLJournalEntry
    entries = (await db.execute(select(GLJournalEntry))).scalars().all()
    assert len(entries) == 1


from app.core.gl_postings import post_order_refund


@pytest.mark.asyncio
async def test_full_void_reverses_sale(db):
    await _seeded(db)
    order = await _make_order(db)
    await gl_postings_post_sale(db, order, _settings(on=True), "u1")
    rev = await post_order_refund(db, order, _settings(on=True), "u1", refunded_item=None)
    assert rev is not None and rev.reverses_entry_id is not None
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    assert tb["total_base_debit"] == tb["total_base_credit"]
    accts = {a["system_key"]: a for a in tb["accounts"]}
    assert accts["CASH"]["net_base"] == D("0.00")
    assert accts["METAL_INVENTORY"]["metal_by_karat"]["K21"]["net_grams"] == D("0.000")


@pytest.mark.asyncio
async def test_full_void_flag_off_noop(db):
    await _seeded(db)
    order = await _make_order(db)
    assert await post_order_refund(db, order, _settings(on=False), "u1", refunded_item=None) is None


@pytest.mark.asyncio
async def test_partial_refund_reverses_only_that_event(db):
    await _seeded(db)
    # Order with a 2-unit coin line @ 50 each = 100 subtotal, vat 11.
    order = Order(
        order_number="ORD-2", cashier_id="u1", payment_method=PaymentMethod.CASH,
        subtotal=D("100"), vat_percent=D("11"), vat_amount=D("11"), discount_percent=D("0"),
        discount_amount=D("0"), total_usd=D("111"), total_lbp=D("0"), lbp_exchange_rate=D("89500"),
    )
    item = OrderItem(item_kind=OrderItemKind.COIN, product_code="C", product_name="Coin",
                     karat=Karat.K21, weight_grams=D("10.000"), gold_rate_at_sale=D("60.00"),
                     margin_percent=D("0"), making_charge=D("0"), final_price=D("100"), quantity=2)
    order.items = [item]
    db.add(order); await db.flush()
    await gl_postings_post_sale(db, order, _settings(on=True), "u1")

    # Refund 1 of 2 units: pre-VAT value 50, qty 1.
    rev = await post_order_refund(db, order, _settings(on=True), "u1",
                                  refunded_item=item, refund_value=D("50"), refund_qty=1, refund_seq=1)
    assert rev is not None
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    assert tb["balanced"] and tb["metal_balanced"]
    accts = {a["system_key"]: a for a in tb["accounts"]}
    # Net cash = 111 in − (50 + 5.50 vat) out = 55.50; net inventory grams =
    # -20 (2 coins @10g sold) + 10 (one coin back) = -10
    assert accts["CASH"]["net_base"] == D("55.50")
    assert accts["METAL_INVENTORY"]["metal_by_karat"]["K21"]["net_grams"] == D("-10.000")

    # Idempotent on the same refund_seq.
    again = await post_order_refund(db, order, _settings(on=True), "u1",
                                    refunded_item=item, refund_value=D("50"), refund_qty=1, refund_seq=1)
    assert again is None


async def _make_order_discounted(db):
    # subtotal 100, discount 10 → gross sales 100, discounts 10, vat 11, total 101.
    order = Order(
        order_number="ORD-D", cashier_id="u1", payment_method=PaymentMethod.CASH,
        subtotal=D("100"), vat_percent=D("11"), vat_amount=D("11"),
        discount_percent=D("10"), discount_amount=D("10"),
        total_usd=D("101"), total_lbp=D("0"), lbp_exchange_rate=D("89500"),
    )
    order.items = [OrderItem(
        item_kind=OrderItemKind.COIN, product_code="C1", product_name="Coin", karat=Karat.K21,
        weight_grams=D("10.000"), gold_rate_at_sale=D("60.00"), margin_percent=D("0"),
        making_charge=D("0"), final_price=D("100"), quantity=1,
    )]
    db.add(order); await db.flush()
    return order


@pytest.mark.asyncio
async def test_post_sale_splits_discount_to_contra_revenue(db):
    await _seeded(db)
    order = await _make_order_discounted(db)
    await gl_postings_post_sale(db, order, _settings(on=True), "u1")
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    assert tb["balanced"] is True and tb["metal_balanced"] is True
    accts = {a["system_key"]: a for a in tb["accounts"]}
    assert accts["SALES_REVENUE"]["base_credit"] == D("100.00")   # GROSS, not net of discount
    assert accts["SALES_DISCOUNTS"]["base_debit"] == D("10.00")   # contra-revenue
    assert accts["CASH"]["base_debit"] == D("101.00")
    assert accts["VAT_PAYABLE"]["base_credit"] == D("11.00")


@pytest.mark.asyncio
async def test_partial_refund_reverses_discount_prorata(db):
    await _seeded(db)
    # 2-unit line @50 = subtotal 100, 10% discount = 10, vat 11, total 101.
    order = Order(
        order_number="ORD-DR", cashier_id="u1", payment_method=PaymentMethod.CASH,
        subtotal=D("100"), vat_percent=D("11"), vat_amount=D("11"),
        discount_percent=D("10"), discount_amount=D("10"),
        total_usd=D("101"), total_lbp=D("0"), lbp_exchange_rate=D("89500"),
    )
    item = OrderItem(item_kind=OrderItemKind.COIN, product_code="C", product_name="Coin",
                     karat=Karat.K21, weight_grams=D("10.000"), gold_rate_at_sale=D("60.00"),
                     margin_percent=D("0"), making_charge=D("0"), final_price=D("100"), quantity=2)
    order.items = [item]
    db.add(order); await db.flush()
    await gl_postings_post_sale(db, order, _settings(on=True), "u1")

    # Refund 1 of 2 units: pre-VAT slice 50. discount_slice = 50/100*10 = 5.
    rev = await post_order_refund(db, order, _settings(on=True), "u1",
                                  refunded_item=item, refund_value=D("50"), refund_qty=1, refund_seq=1)
    assert rev is not None
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    assert tb["balanced"] and tb["metal_balanced"]
    accts = {a["system_key"]: a for a in tb["accounts"]}
    # Discounts net: 10 (sale) − 5 (refund reversal) = 5 remaining debit.
    assert accts["SALES_DISCOUNTS"]["net_base"] == D("5.00")
    # Cash net: 101 in − (50 − 5 + 5.50) out = 101 − 50.50 = 50.50.
    assert accts["CASH"]["net_base"] == D("50.50")


@pytest.mark.asyncio
async def test_card_sale_debits_clearing_not_bank(db):
    await _seeded(db)
    order = await _make_order(db, payment="CARD")
    await gl_postings_post_sale(db, order, _settings(on=True), "u1")
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    accts = {a["system_key"]: a for a in tb["accounts"]}
    assert accts["CREDIT_CARD_CLEARING"]["base_debit"] == D("111.00")
    assert accts.get("BANK", {}).get("base_debit", D("0")) == D("0.00")
