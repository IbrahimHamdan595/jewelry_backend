from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.core import dashboard
from app.core.daterange import BEIRUT_TZ, day_range


def test_dashboard_module_importable():
    assert hasattr(dashboard, "week_window")


def test_week_window_is_seven_beirut_days():
    start, end = dashboard.week_window(date(2026, 6, 5))
    assert end == day_range(date(2026, 6, 5))[1]
    assert start == day_range(date(2026, 5, 30))[0]


# ── shared fixtures ───────────────────────────────────────────────────────────
from app.models import (
    Karat, Order, OrderItem, OrderItemKind, OrderStatus, PaymentMethod, User,
)

_SEQ = [0]


async def _ensure_user(db):
    u = (await db.execute(select(User).where(User.id == "u1"))).scalar_one_or_none()
    if u is None:
        db.add(User(id="u1", name="C", email="c@x.co", password_hash="x", role="ADMIN"))
        await db.flush()


async def _order(db, *, total, when, status=OrderStatus.COMPLETED, items=()):
    await _ensure_user(db)
    _SEQ[0] += 1
    o = Order(cashier_id="u1", order_number=f"O{_SEQ[0]}", status=status,
              payment_method=PaymentMethod.CASH, subtotal=total, vat_percent=D("0"),
              vat_amount=D("0"), total_usd=total, total_lbp=D("0"),
              lbp_exchange_rate=D("89500"), created_at=when)
    db.add(o)
    await db.flush()
    for it in items:
        db.add(OrderItem(order_id=o.id, item_kind=OrderItemKind.PRODUCT, quantity=it["qty"],
                         product_code="P", product_name="P", karat=it["karat"],
                         weight_grams=it["grams"], gold_rate_at_sale=D("80"),
                         margin_percent=D("0"), making_charge=it["making"],
                         final_price=it["final"]))
    await db.flush()
    return o


# ── Phase A ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gold_weight_sold_by_karat(db):
    now = day_range(date(2026, 6, 5))[0] + timedelta(hours=12)
    await _order(db, total=D("500"), when=now, items=[
        {"qty": 2, "karat": Karat.K21, "grams": D("5"), "making": D("25"), "final": D("250")},
        {"qty": 1, "karat": Karat.K18, "grams": D("3"), "making": D("25"), "final": D("100")},
    ])
    rows = await dashboard.gold_weight_sold_by_karat(db, *day_range(date(2026, 6, 5)))
    by = {r["karat"]: r["grams"] for r in rows}
    assert by["K21"] == D("10.000")
    assert by["K18"] == D("3.000")


@pytest.mark.asyncio
async def test_making_charges_and_avg_invoice(db):
    now = day_range(date(2026, 6, 5))[0] + timedelta(hours=12)
    await _order(db, total=D("300"), when=now, items=[
        {"qty": 2, "karat": Karat.K21, "grams": D("5"), "making": D("25"), "final": D("250")}])
    making = await dashboard.making_charges(db, *day_range(date(2026, 6, 5)))
    assert making == D("50.00")
    assert dashboard.avg_invoice(D("300"), 1) == D("300.00")
    assert dashboard.avg_invoice(D("0"), 0) == D("0.00")


# ── Phase D ───────────────────────────────────────────────────────────────────
from app.models import (
    CoinType, GoldLot, LotSource, MarginMode, OunceType, Product, ProductStatus,
)


@pytest.mark.asyncio
async def test_inventory_value_market(db):
    db.add(GoldLot(karat=Karat.K24, weight_grams=D("10"), weight_remaining_grams=D("10"),
                   source=LotSource.SEED, cost_basis_usd=D("700"), is_depleted=False))
    db.add(Product(code="R1", name_en="R", category="rings", karat=Karat.K21, weight_grams=D("4"),
                   margin_percent=D("15"), making_charge=D("25"), on_hand_qty=2,
                   status=ProductStatus.AVAILABLE, cost_basis_usd=D("250")))
    await db.flush()
    val = await dashboard.inventory_valuation(db, rate_24k=D("80"))
    assert val["pure_gold_usd"] == D("799.20")   # 10g K24 × 0.999 × 80
    assert val["products_usd"] == D("500.00")     # 2 × 250
    assert val["total_usd"] == val["pure_gold_usd"] + val["coins_usd"] + val["ounces_usd"] + val["products_usd"]
    assert val["method"] == "market"


@pytest.mark.asyncio
async def test_inventory_aging_dead_stock_low_stock(db):
    asof = datetime.now(BEIRUT_TZ)
    old = asof - timedelta(days=400)
    recent = asof - timedelta(days=10)
    db.add(GoldLot(karat=Karat.K24, weight_grams=D("1"), weight_remaining_grams=D("1"),
                   source=LotSource.SEED, cost_basis_usd=D("70"), is_depleted=False, acquired_at=old))
    db.add(GoldLot(karat=Karat.K24, weight_grams=D("1"), weight_remaining_grams=D("1"),
                   source=LotSource.SEED, cost_basis_usd=D("70"), is_depleted=False, acquired_at=recent))
    db.add(Product(code="L", name_en="L", category="rings", karat=Karat.K21, weight_grams=D("4"),
                   margin_percent=D("15"), making_charge=D("25"), on_hand_qty=1, min_stock_qty=5,
                   status=ProductStatus.AVAILABLE, created_at=old))
    await db.flush()
    aging = await dashboard.inventory_aging(db, asof=asof)
    assert aging["d365_plus"] >= 1 and aging["d0_90"] >= 1
    assert (await dashboard.low_stock_count(db)) >= 1            # product below min_stock
    assert (await dashboard.dead_stock_count(db, asof=asof)) >= 1  # 400-day-old in-stock product


# ── Phase B ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gl_has_entries_false_when_dormant(db):
    assert (await dashboard.gl_has_entries(db)) is False


@pytest.mark.asyncio
async def test_receivables_payables_shape(db):
    r = await dashboard.receivables(db, as_of=date(2026, 6, 5))
    assert set(r) == {"total", "b0_30", "b31_60", "b61_90", "b90_plus"}
    assert r["total"] == 0.0
    p = await dashboard.payables_aging(db, as_of=date(2026, 6, 5))
    assert set(p) == {"cash_total", "b0_30", "b31_60", "b61_90", "b90_plus", "metal_owed_by_karat"}
    assert p["cash_total"] == 0.0


# ── Phase E ───────────────────────────────────────────────────────────────────
from app.core import ledger
from app.models import InventoryLedger, Settings


@pytest.mark.asyncio
async def test_loss_prevention_counts(db):
    await _ensure_user(db)
    start, end = day_range(date(2026, 6, 5))
    mid = start + timedelta(hours=6)
    db.add(InventoryLedger(event_type=ledger.EVENT_ORDER_VOID, actor_user_id="u1",
                           ref_type="order", ref_id="o1", payload={}, occurred_at=mid,
                           prev_hash="x", entry_hash="hash-void"))
    db.add(InventoryLedger(event_type=ledger.EVENT_GOLD_RATE_OVERRIDE_SET, actor_user_id="u1",
                           ref_type="rate", ref_id="r1", payload={}, occurred_at=mid,
                           prev_hash="hash-void", entry_hash="hash-override"))
    db.add(Settings(id="singleton", max_discount_percent=D("10")))
    await db.flush()
    await _order(db, total=D("100"), when=mid)                 # no discount
    o = await _order(db, total=D("100"), when=mid)             # 15% > 10% threshold
    o.discount_percent = D("15")
    await db.flush()
    lp = await dashboard.loss_prevention(db, start, end)
    assert lp["order_voids"] == 1
    assert lp["rate_overrides"] == 1
    assert lp["excess_discount_orders"] == 1


# ── Phase C ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_profitability_excludes_null_cost(db):
    now = day_range(date(2026, 6, 5))[0] + timedelta(hours=12)
    o1 = await _order(db, total=D("250"), when=now, items=[
        {"qty": 1, "karat": Karat.K21, "grams": D("5"), "making": D("25"), "final": D("250")}])
    it = (await db.execute(select(OrderItem).where(OrderItem.order_id == o1.id))).scalar_one()
    it.cost_basis_usd = D("180")                 # cost captured
    await _order(db, total=D("300"), when=now, items=[
        {"qty": 1, "karat": Karat.K21, "grams": D("4"), "making": D("25"), "final": D("300")}])
    await db.flush()                              # o2 item leaves cost NULL → excluded
    p = await dashboard.profitability(db, *day_range(date(2026, 6, 5)))
    assert p["gross_profit"] == D("70.00")        # 250 − 180
    assert p["gross_margin_pct"] == D("28.00")    # 70 / 250
    assert p["profit_per_gram"] == D("14.00")     # 70 / 5


@pytest.mark.asyncio
async def test_profitability_none_when_no_cost_captured(db):
    now = day_range(date(2026, 6, 5))[0] + timedelta(hours=12)
    await _order(db, total=D("300"), when=now, items=[
        {"qty": 1, "karat": Karat.K21, "grams": D("4"), "making": D("25"), "final": D("300")}])
    assert await dashboard.profitability(db, *day_range(date(2026, 6, 5))) is None


# ── Phase B — cash & bank balance is one SQL SUM (NEX-53) ─────────────────────
from sqlalchemy import event

from app.core import gl
from app.core.bank import adopt_seeded_accounts
from app.core.coa_seed import seed_chart_of_accounts
from app.models import BankAccount, GLAccount, GLJournalLine, GLPeriod, PeriodStatus


async def _cash_bank_balance_replay(db):
    """The helper as it was before NEX-53: load every line of every active
    bank account and sum in Python. Oracle for the SQL aggregate."""
    accts = (await db.execute(select(BankAccount).where(BankAccount.is_active.is_(True)))).scalars().all()
    total = D("0")
    for ba in accts:
        rows = (await db.execute(
            select(GLJournalLine).where(GLJournalLine.account_id == ba.gl_account_id))).scalars().all()
        total += sum((l.base_debit - l.base_credit for l in rows), D("0"))
    return total.quantize(D("0.01"))


@pytest.mark.asyncio
async def test_cash_bank_balance_matches_python_replay_in_one_statement(db):
    await _ensure_user(db)
    await seed_chart_of_accounts(db)
    await adopt_seeded_accounts(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    await db.flush()

    async def acct(key):
        return (await db.execute(select(GLAccount).where(GLAccount.system_key == key))).scalar_one()

    cash, cash_lbp, bank, petty, equity = [
        await acct(k) for k in ("CASH", "CASH_LBP", "BANK", "CASH_PETTY", "OPENING_BALANCE_EQUITY")]
    # Petty cash is retired: its lines must not count.
    (await db.execute(select(BankAccount).where(BankAccount.gl_account_id == petty.id))).scalar_one().is_active = False

    async def post(lines):
        await gl.post_entry(db, entry_date=date(2026, 6, 5), memo="t", source_type=gl.SOURCE_MANUAL,
                            source_id=None, actor_user_id="u1", lines=lines)

    def dr(a, amount, **kw):
        return gl.GLLine(account_id=a.id, denomination="MONEY", base_debit=amount,
                         money_debit=kw.pop("money", amount), **kw)

    def cr(a, amount):
        return gl.GLLine(account_id=a.id, denomination="MONEY", base_credit=amount, money_credit=amount)

    await post([dr(cash, D("1000.10")), dr(bank, D("250.05")), dr(petty, D("40.40")),
                dr(cash_lbp, D("11.17"), money=D("1000000.00"), currency="LBP", fx_rate=D("89500")),
                cr(equity, D("1301.72"))])
    await post([dr(bank, D("300.00")), cr(cash, D("300.00"))])       # transfer nets to zero
    for _ in range(30):                                               # 30 × 0.10 defeats float sums
        await post([dr(cash, D("0.10")), cr(equity, D("0.10"))])
    await post([dr(equity, D("75.33")), cr(bank, D("75.33"))])        # a withdrawal

    expected = await _cash_bank_balance_replay(db)
    assert expected == D("1188.99")

    statements = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.bind.sync_engine, "before_cursor_execute", _capture)
    try:
        actual = await dashboard.cash_bank_balance(db)
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", _capture)

    assert actual == expected and str(actual) == "1188.99"
    assert isinstance(actual, D)
    assert len(statements) == 1          # was one query per bank account, each fetching every line


@pytest.mark.asyncio
async def test_cash_bank_balance_is_zero_without_accounts_or_lines(db):
    assert str(await dashboard.cash_bank_balance(db)) == "0.00"       # no bank accounts at all
    await seed_chart_of_accounts(db)
    await adopt_seeded_accounts(db)
    assert str(await dashboard.cash_bank_balance(db)) == "0.00"       # accounts, dormant GL
