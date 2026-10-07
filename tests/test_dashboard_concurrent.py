"""NEX-53 — the dashboard fans its sections out over separate sessions.

The oracle is tests/dashboard_oracle.py: the pre-refactor handler, every query
in sequence on one session. The concurrent path must produce the same bytes.

In-memory SQLite gives every connection its own empty database, so these tests
run on a file-backed SQLite engine instead: each concurrent session really is a
separate connection reading the same committed data.
"""
import asyncio
import re
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401  (registers every table on Base.metadata)
from app.api import reports
from app.core import dashboard as dash
from app.core import gl, ledger
from app.core.audit_chain import GENESIS_HASH
from app.core.bank import adopt_seeded_accounts
from app.core.coa_seed import seed_chart_of_accounts
from app.core.daterange import day_range
from app.db.base import Base
from app.models import (
    ARInvoice, ARInvoiceStatus, AuthAuditChainHead, BankAccount, CoinType, Customer, DebtUnit,
    GLAccount, GLJournalChainHead, GLPeriod, GoldLot, GoldRateHistory, GoldRateOverride,
    InventoryLedger, InventoryLedgerChainHead, Karat, LotSource, MarginMode, Order, OrderItem,
    OrderItemKind, OrderStatus, OunceType, PaymentMethod, PeriodStatus, Product, ProductStatus,
    Role, Settings, Supplier, SupplierBalance, SupplierItemKind, SupplierPayment,
    SupplierPurchase, SupplierPurchaseItem, SupplierPurchaseMode, User,
)
from tests.dashboard_oracle import legacy_dashboard

UTC = timezone.utc
# 22:30 UTC on the 15th is 01:30 on the 16th in Beirut (UTC+3 in June): the UTC
# and Beirut calendar days differ, so a window computed on the wrong clock
# shifts every "today" figure.
NOW = datetime(2026, 6, 15, 22, 30, tzinfo=UTC)
TODAY = date(2026, 6, 16)


def _body(payload: dict) -> bytes:
    """The bytes FastAPI puts on the wire for a handler returning `payload`."""
    return JSONResponse(jsonable_encoder(payload)).body


def _ago(**delta) -> datetime:
    return NOW - timedelta(**delta)


# ── session tracking ──────────────────────────────────────────────────────────

class _Tracker:
    def __init__(self):
        self.opened = 0      # sessions entered in total
        self.open = 0        # sessions currently inside `async with`
        self.peak = 0        # most sessions open at the same moment
        self.overlaps = 0    # execute() entered while the same session was busy

    def reset(self):
        self.__init__()


def _tracked_sessions(engine, tracker: _Tracker) -> async_sessionmaker:
    class TrackedSession(AsyncSession):
        _busy = False

        async def __aenter__(self):
            tracker.opened += 1
            tracker.open += 1
            tracker.peak = max(tracker.peak, tracker.open)
            return await super().__aenter__()

        async def __aexit__(self, *exc):
            try:
                return await super().__aexit__(*exc)
            finally:
                tracker.open -= 1

        async def execute(self, *args, **kwargs):
            if self._busy:
                tracker.overlaps += 1
            self._busy = True
            try:
                return await super().execute(*args, **kwargs)
            finally:
                self._busy = False

    return async_sessionmaker(engine, expire_on_commit=False, class_=TrackedSession)


class _Statements:
    """Counts SQL statements — one per DB round-trip — while active."""

    def __init__(self, engine):
        self._engine = engine.sync_engine
        self.count = 0

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany):
        self.count += 1

    def __enter__(self):
        event.listen(self._engine, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *exc):
        event.remove(self._engine, "before_cursor_execute", self._on_execute)


# ── fixture data ──────────────────────────────────────────────────────────────

def _order(number, when, total, *, cashier="u1", status=OrderStatus.COMPLETED,
           discount=D("0"), items=()):
    o = Order(order_number=number, cashier_id=cashier, status=status,
              payment_method=PaymentMethod.CASH, subtotal=total, vat_percent=D("0"),
              vat_amount=D("0"), discount_percent=discount, total_usd=total,
              total_lbp=total * D("89500"), lbp_exchange_rate=D("89500"), created_at=when)
    o.items = [
        OrderItem(item_kind=OrderItemKind.PRODUCT, quantity=qty, product_code=code,
                  product_name=code.title(), karat=karat, weight_grams=grams,
                  gold_rate_at_sale=D("78.43"), margin_percent=D("15"), making_charge=making,
                  final_price=final, cost_basis_usd=cost)
        for (code, karat, qty, grams, making, final, cost) in items
    ]
    return o


def _product(code, karat, grams, *, qty, age_days, min_qty=None, cost=None,
             status=ProductStatus.AVAILABLE, active=True):
    return Product(code=code, name_en=code, category="rings", karat=karat, weight_grams=grams,
                   margin_percent=D("15"), making_charge=D("25"), on_hand_qty=qty,
                   min_stock_qty=min_qty, cost_basis_usd=cost, status=status, is_active=active,
                   created_at=_ago(days=age_days))


def _purchase(supplier, when, due, *, paid=D("0"), n_items=1):
    p = SupplierPurchase(supplier_id=supplier.id, occurred_at=when,
                         payment_mode=SupplierPurchaseMode.CASH, total_cash_due=due,
                         total_grams_due_by_karat={}, cash_paid_at_creation=paid,
                         grams_paid_at_creation_by_karat={}, created_by_user_id="u1")
    p.items = [SupplierPurchaseItem(item_kind=SupplierItemKind.PRODUCT, unit_cost_usd=due)
               for _ in range(n_items)]
    return p


def _ledger_event(n, event_type, when):
    return InventoryLedger(event_type=event_type, actor_user_id="u1", ref_type="order",
                           ref_id=f"ref-{n}", payload={}, occurred_at=when,
                           prev_hash=f"seed-prev-{n}", entry_hash=f"seed-hash-{n}")


async def _acct(db, key: str) -> GLAccount:
    return (await db.execute(select(GLAccount).where(GLAccount.system_key == key))).scalar_one()


def _dr(acct, amount, **kw):
    return gl.GLLine(account_id=acct.id, denomination="MONEY", base_debit=amount,
                     money_debit=kw.pop("money", amount), **kw)


def _cr(acct, amount, **kw):
    return gl.GLLine(account_id=acct.id, denomination="MONEY", base_credit=amount,
                     money_credit=kw.pop("money", amount), **kw)


async def _seed_gold_rate(db):
    db.add_all([
        GoldRateHistory(rate_24k=D("77.00"), rate_22k=D("70.61"), rate_21k=D("67.38"),
                        rate_18k=D("57.75"), source="goldapi", fetched_at=_ago(days=1)),
        GoldRateHistory(rate_24k=D("78.43"), rate_22k=D("71.92"), rate_21k=D("68.63"),
                        rate_18k=D("58.82"), source="goldapi", fetched_at=_ago(minutes=10)),
    ])


async def _seed_shop(db):
    """A shop with a live GL: sales on both sides of every window boundary,
    stock of every kind and age, suppliers and customers owing in every bucket."""
    db.add_all([
        User(id="u1", name="Rima", email="rima@x.co", password_hash="x", role=Role.ADMIN),
        User(id="u2", name="Karim", email="karim@x.co", password_hash="x", role=Role.CASHIER),
        Settings(id="singleton", max_discount_percent=D("10")),
    ])
    await db.flush()
    await _seed_gold_rate(db)

    # ── sales ────────────────────────────────────────────────────────────────
    ring = ("RNG-21", Karat.K21)
    chain = ("CHN-18", Karat.K18)
    coin = ("COIN-24", Karat.K24)
    bracelet = ("BRC-22", Karat.K22)
    db.add_all([
        _order("ORD-0001", datetime(2026, 5, 1, 9, 0, tzinfo=UTC), D("50.00")),
        # previous Beirut week is 3–9 June: 21:00 UTC on the 2nd is its first instant
        _order("ORD-0002", datetime(2026, 6, 2, 20, 59, 59, tzinfo=UTC), D("70.07")),
        _order("ORD-0003", datetime(2026, 6, 2, 21, 0, 0, tzinfo=UTC), D("800.30"),
               items=[("OLD-21", Karat.K21, 1, D("9.000"), D("30.00"), D("800.30"), D("600.00"))]),
        _order("ORD-0004", datetime(2026, 6, 7, 10, 0, tzinfo=UTC), D("199.70"), cashier="u2",
               discount=D("20")),
        _order("ORD-0005", datetime(2026, 6, 9, 20, 59, 59, tzinfo=UTC), D("10.01")),
        # this Beirut week is 10–16 June: 21:00 UTC on the 9th is its first instant
        _order("ORD-0006", datetime(2026, 6, 9, 21, 0, 0, tzinfo=UTC), D("310.07"), cashier="u2",
               items=[(*coin, 3, D("8.000"), D("1.10"), D("310.07"), None)]),
        _order("ORD-0007", datetime(2026, 6, 12, 10, 15, tzinfo=UTC), D("420.42"),
               items=[(*ring, 1, D("5.250"), D("25.00"), D("320.32"), D("260.10")),
                      (*chain, 1, D("3.125"), D("15.50"), D("100.10"), D("71.03"))]),
        _order("ORD-0008", datetime(2026, 6, 13, 8, 0, tzinfo=UTC), D("150.00"),
               status=OrderStatus.REFUNDED,
               items=[(*ring, 1, D("5.250"), D("25.00"), D("150.00"), D("120.00"))]),
        # 23:59:59 on the 15th in Beirut — the same UTC date as NOW, but yesterday
        _order("ORD-0009", datetime(2026, 6, 15, 20, 59, 59, tzinfo=UTC), D("99.99"), cashier="u2",
               discount=D("12.5"),
               items=[(*ring, 1, D("5.250"), D("25.00"), D("66.66"), D("60.01")),
                      (*chain, 1, D("3.125"), D("15.50"), D("33.33"), None)]),
        # 00:00:00 on the 16th in Beirut — the first instant of today
        _order("ORD-0010", datetime(2026, 6, 15, 21, 0, 0, tzinfo=UTC), D("0.10"),
               items=[(*coin, 1, D("8.000"), D("0.01"), D("0.10"), D("0.07"))]),
        _order("ORD-0011", datetime(2026, 6, 15, 21, 30, tzinfo=UTC), D("1234.56"),
               items=[(*ring, 1, D("5.250"), D("25.00"), D("700.10"), D("520.40")),
                      (*chain, 2, D("3.125"), D("15.50"), D("534.46"), D("401.11"))]),
        _order("ORD-0012", datetime(2026, 6, 15, 22, 10, tzinfo=UTC), D("500.00"),
               status=OrderStatus.VOIDED, discount=D("50"),
               items=[(*ring, 1, D("5.250"), D("25.00"), D("500.00"), D("400.00"))]),
        _order("ORD-0013", datetime(2026, 6, 15, 22, 20, tzinfo=UTC), D("0.20"), cashier="u2",
               discount=D("15"),
               items=[(*bracelet, 1, D("1.111"), D("0.05"), D("0.13"), D("0.11")),
                      (*ring, 1, D("0.333"), D("0.02"), D("0.07"), D("0.04"))]),
    ])
    db.add_all([
        _ledger_event(1, ledger.EVENT_ORDER_VOID, datetime(2026, 6, 1, 9, 0, tzinfo=UTC)),
        _ledger_event(2, ledger.EVENT_ORDER_VOID, datetime(2026, 6, 9, 21, 0, 0, tzinfo=UTC)),
        _ledger_event(3, ledger.EVENT_ORDER_VOID, datetime(2026, 6, 11, 9, 0, tzinfo=UTC)),
        _ledger_event(4, ledger.EVENT_ORDER_VOID, datetime(2026, 6, 15, 22, 11, tzinfo=UTC)),
        _ledger_event(5, ledger.EVENT_GOLD_RATE_OVERRIDE_SET, datetime(2026, 6, 14, 7, 0, tzinfo=UTC)),
    ])

    # ── stock ────────────────────────────────────────────────────────────────
    def lot(karat, remaining, age_days, depleted=False):
        return GoldLot(karat=karat, weight_grams=remaining + D("10"), weight_remaining_grams=remaining,
                       source=LotSource.SEED, cost_basis_usd=D("700"), is_depleted=depleted,
                       acquired_at=_ago(days=age_days))

    def unit(model, code, karat, grams, qty, min_qty=None, active=True):
        return model(code=code, name_en=code, karat=karat, weight_grams=grams,
                     margin_mode=MarginMode.USD, on_hand_qty=qty, min_stock_qty=min_qty,
                     is_active=active)

    db.add_all([
        lot(Karat.K24, D("100.123"), 400), lot(Karat.K21, D("55.555"), 100),
        lot(Karat.K21, D("10.001"), 10), lot(Karat.K22, D("7.777"), 200),
        lot(Karat.K18, D("0.000"), 50, depleted=True),
        unit(CoinType, "LIRA-8", Karat.K21, D("8.000"), 12, min_qty=5),
        unit(CoinType, "LIRA-4", Karat.K21, D("4.000"), 2, min_qty=3),
        unit(CoinType, "LIRA-OLD", Karat.K22, D("7.988"), 9, min_qty=20, active=False),
        unit(OunceType, "OZ-1", Karat.K24, D("31.104"), 4, min_qty=5),
        unit(OunceType, "OZ-10G", Karat.K24, D("10.000"), 20),
        _product("RNG-21", Karat.K21, D("5.250"), qty=3, age_days=30, min_qty=1, cost=D("520.40")),
        _product("CHN-18", Karat.K18, D("3.125"), qty=1, age_days=95, min_qty=2),
        _product("BRC-22", Karat.K22, D("12.340"), qty=2, age_days=400, cost=D("900.00")),
        _product("PND-21", Karat.K21, D("2.220"), qty=1, age_days=200),
        _product("EDGE-365", Karat.K21, D("1.000"), qty=1, age_days=365),
        _product("EDGE-366", Karat.K21, D("1.000"), qty=1, age_days=366),
        _product("SOLD-OUT", Karat.K18, D("4.000"), qty=0, age_days=500, min_qty=1,
                 status=ProductStatus.SOLD),
        _product("MELTED", Karat.K21, D("6.000"), qty=1, age_days=500, min_qty=5,
                 status=ProductStatus.MELTED),
        _product("RETIRED", Karat.K21, D("6.000"), qty=1, age_days=500,
                 status=ProductStatus.INACTIVE),
        _product("HIDDEN", Karat.K21, D("6.000"), qty=1, age_days=500, min_qty=5, active=False),
    ])

    # ── suppliers / AP ───────────────────────────────────────────────────────
    bullion, sidon, idle, settled = (Supplier(name=n) for n in
                                     ("Beirut Bullion", "Sidon Gold", "Idle Supplier", "Settled Co"))
    db.add_all([bullion, sidon, idle, settled])
    await db.flush()
    db.add_all([
        _purchase(bullion, _ago(days=120), D("1000.00"), paid=D("200.00"), n_items=2),
        _purchase(bullion, _ago(days=70), D("500.50")),
        _purchase(bullion, _ago(days=40), D("300.25"), paid=D("0.25"), n_items=3),
        _purchase(bullion, _ago(days=5), D("150.00")),
        _purchase(sidon, _ago(days=95), D("2000.00")),
        _purchase(sidon, _ago(days=1), D("75.75"), n_items=4),
        _purchase(settled, _ago(days=20), D("60.00"), paid=D("60.00")),
        # FIFO: 900 clears the 800 left on the 120-day purchase, then 100 of the 70-day one.
        SupplierPayment(supplier_id=bullion.id, unit=DebtUnit.CASH, amount=D("900.00"),
                        paid_by_user_id="u1", paid_at=_ago(days=30)),
        SupplierPayment(supplier_id=bullion.id, unit=DebtUnit.GOLD, karat=Karat.K21,
                        amount=D("5.000"), paid_by_user_id="u1", paid_at=_ago(days=3)),
        SupplierBalance(supplier_id=bullion.id, unit=DebtUnit.CASH, karat="", balance=D("850.50")),
        SupplierBalance(supplier_id=bullion.id, unit=DebtUnit.GOLD, karat="K21", balance=D("30.500")),
        SupplierBalance(supplier_id=bullion.id, unit=DebtUnit.GOLD, karat="K18", balance=D("0.000")),
        SupplierBalance(supplier_id=sidon.id, unit=DebtUnit.GOLD, karat="K21", balance=D("12.345")),
        SupplierBalance(supplier_id=sidon.id, unit=DebtUnit.GOLD, karat="K24", balance=D("1.001")),
    ])

    # ── customers / AR ───────────────────────────────────────────────────────
    walid, nour = Customer(name="Walid"), Customer(name="Nour")
    db.add_all([walid, nour])
    await db.flush()

    def invoice(no, customer, age_days, total, paid, status):
        return ARInvoice(invoice_no=no, customer_id=customer.id,
                         invoice_date=TODAY - timedelta(days=age_days), total=total,
                         subtotal=total, amount_paid=paid, status=status)

    db.add_all([
        invoice("AR-1", walid, 10, D("1500.00"), D("0"), ARInvoiceStatus.OPEN),
        invoice("AR-2", walid, 45, D("800.80"), D("300.30"), ARInvoiceStatus.PARTIAL),
        invoice("AR-3", nour, 75, D("250.25"), D("0"), ARInvoiceStatus.OPEN),
        invoice("AR-4", nour, 120, D("999.99"), D("0.01"), ARInvoiceStatus.PARTIAL),
        invoice("AR-5", nour, 5, D("100.00"), D("100.00"), ARInvoiceStatus.PAID),
        invoice("AR-6", walid, 30, D("10.10"), D("0"), ARInvoiceStatus.OPEN),
        invoice("AR-7", walid, 15, D("444.44"), D("0"), ARInvoiceStatus.VOID),
    ])

    # ── general ledger ───────────────────────────────────────────────────────
    await seed_chart_of_accounts(db)
    await adopt_seeded_accounts(db)
    (await db.execute(select(BankAccount).join(GLAccount, BankAccount.gl_account_id == GLAccount.id)
                      .where(GLAccount.system_key == "CASH_PETTY"))).scalar_one().is_active = False
    for month in (3, 5, 6):
        db.add(GLPeriod(year=2026, period_no=month, status=PeriodStatus.OPEN))
    await db.flush()
    cash, cash_lbp, bank, petty = [await _acct(db, k) for k in ("CASH", "CASH_LBP", "BANK", "CASH_PETTY")]
    equity, sales, rent = [await _acct(db, k) for k in ("OPENING_BALANCE_EQUITY", "SALES_REVENUE", "RENT_EXPENSE")]
    vat_out, vat_in = await _acct(db, "VAT_PAYABLE"), await _acct(db, "VAT_RECEIVABLE")

    async def post(entry_date, lines):
        return await gl.post_entry(db, entry_date=entry_date, memo="seed", source_type=gl.SOURCE_MANUAL,
                                   source_id=None, actor_user_id="u1", lines=lines)

    # Q1 sale: its VAT belongs to the previous quarter's return.
    await post(date(2026, 3, 10), [_dr(cash, D("111.00")), _cr(sales, D("100.00")), _cr(vat_out, D("11.00"))])
    await post(date(2026, 5, 2), [
        _dr(cash, D("5000.00")), _dr(bank, D("12000.50")), _dr(petty, D("40.40")),
        _dr(cash_lbp, D("100.00"), money=D("8950000.00"), currency="LBP", fx_rate=D("89500")),
        _cr(equity, D("17140.90")),
    ])
    await post(date(2026, 6, 10), [_dr(cash, D("1234.56")), _cr(sales, D("1112.22")), _cr(vat_out, D("122.34"))])
    await post(date(2026, 6, 12), [_dr(rent, D("500.00")), _dr(vat_in, D("55.00")), _cr(bank, D("555.00"))])
    wrong = await post(date(2026, 6, 13), [_dr(cash, D("110.99")), _cr(sales, D("99.99")), _cr(vat_out, D("11.00"))])
    await gl.reverse_entry(db, original_entry_id=wrong.id, actor_user_id="u1", entry_date=date(2026, 6, 14))
    await post(date(2026, 6, 14), [_dr(bank, D("300.00")), _cr(cash, D("300.00"))])


async def _engine_with_schema(path, **engine_kwargs):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", echo=False, **engine_kwargs)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


async def _make_shop(path, seed, **engine_kwargs):
    engine = await _engine_with_schema(path, **engine_kwargs)
    tracker = _Tracker()
    sessions = _tracked_sessions(engine, tracker)
    async with sessions() as db:
        db.add_all([
            InventoryLedgerChainHead(id=1, latest_entry_hash=GENESIS_HASH, row_count=0),
            AuthAuditChainHead(id=1, latest_entry_hash=GENESIS_HASH, row_count=0),
            GLJournalChainHead(id=1, latest_entry_hash=GENESIS_HASH, row_count=0),
        ])
        await seed(db)
        await db.commit()
    tracker.reset()   # count only what the tests themselves open
    return SimpleNamespace(engine=engine, sessions=sessions, tracker=tracker)


@pytest_asyncio.fixture
async def shop(tmp_path):
    env = await _make_shop(tmp_path / "shop.db", _seed_shop)
    yield env
    await env.engine.dispose()


@pytest_asyncio.fixture
async def dormant_shop(tmp_path):
    """Day one: a gold rate and nothing else — no sales, no stock, GL dormant."""
    env = await _make_shop(tmp_path / "dormant.db", _seed_gold_rate)
    yield env
    await env.engine.dispose()


async def _oracle(env, *, now=NOW) -> dict:
    async with env.sessions() as db:
        return await legacy_dashboard(db, now=now)


# ── byte-identical payload ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_payload_is_byte_identical_to_sequential_oracle(shop):
    expected = await _oracle(shop)
    actual = await reports.build_dashboard(shop.sessions, now=NOW)

    assert _body(actual) == _body(expected)
    assert list(actual) == list(expected)          # same key order, not just same keys

    # The fixture is not vacuous: every block carries real figures.
    assert actual["today_orders"] == 3
    assert actual["week_revenue"] == "2065.34"
    assert len(actual["top_sellers"]) == 4 and len(actual["recent_orders"]) == 5
    assert len(actual["recent_purchases"]) == 5
    assert D(actual["receivables"]["total"]) > 0 and D(actual["payables_aging"]["cash_total"]) > 0
    assert actual["cash_bank_balance"] is not None and actual["vat_position"] is not None
    assert actual["profitability"] is not None
    assert sum(actual["inventory_aging"].values()) > 0 and actual["dead_stock_count"] > 0
    assert all(actual["loss_prevention"].values())
    assert actual["inventory"]["low_stock_alerts"] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2, 3, 4, 8, 64])
async def test_payload_does_not_depend_on_the_concurrency_limit(shop, limit):
    expected = await _oracle(shop)
    actual = await reports.build_dashboard(shop.sessions, now=NOW, max_concurrency=limit)
    assert _body(actual) == _body(expected)


@pytest.mark.asyncio
async def test_dormant_shop_payload_is_byte_identical(dormant_shop):
    expected = await _oracle(dormant_shop)
    actual = await reports.build_dashboard(dormant_shop.sessions, now=NOW)

    assert _body(actual) == _body(expected)
    assert actual["cash_bank_balance"] is None and actual["vat_position"] is None
    assert actual["profitability"] is None and actual["today_orders"] == 0


@pytest.mark.asyncio
async def test_active_rate_override_payload_is_byte_identical(shop):
    async with shop.sessions() as db:
        db.add(GoldRateOverride(rate_24k=D("81.17"), set_by="u1", set_at=_ago(hours=2), is_active=True))
        await db.commit()

    expected = await _oracle(shop)
    actual = await reports.build_dashboard(shop.sessions, now=NOW)

    assert _body(actual) == _body(expected)
    assert actual["gold_rate_is_stale"] is False
    assert actual["inventory_value"]["rate_24k"] == "81.17"  # valued at the override
    assert actual["gold_rate_24k"] == "78.43"                # headline stays the last polled rate


# ── one clock read, Beirut-local windows ──────────────────────────────────────

def test_windows_all_derive_from_one_instant():
    w = dash.windows(NOW)
    assert w.now == NOW
    assert w.today == TODAY                          # Beirut's date, not UTC's (the 15th)
    assert (w.today_start, w.today_end) == day_range(TODAY)
    assert (w.week_start, w.week_end) == (day_range(date(2026, 6, 10))[0], day_range(TODAY)[1])
    assert (w.prev_week_start, w.prev_week_end) == (day_range(date(2026, 6, 3))[0], w.week_start)
    with pytest.raises(FrozenInstanceError):
        w.today = date(2026, 6, 17)


def test_windows_follow_beirut_across_utc_midnight():
    # 20:59:59 UTC is still the 15th in Beirut; one second later it is the 16th.
    assert dash.windows(datetime(2026, 6, 15, 20, 59, 59, tzinfo=UTC)).today == date(2026, 6, 15)
    assert dash.windows(datetime(2026, 6, 15, 21, 0, 0, tzinfo=UTC)).today == TODAY
    # Winter (UTC+2): the Beirut day turns at 22:00 UTC.
    assert dash.windows(datetime(2026, 1, 10, 21, 59, 59, tzinfo=UTC)).today == date(2026, 1, 10)
    assert dash.windows(datetime(2026, 1, 10, 22, 0, 0, tzinfo=UTC)).today == date(2026, 1, 11)


@pytest.mark.asyncio
async def test_day_and_week_boundaries_stay_beirut_local(shop):
    p = await reports.build_dashboard(shop.sessions, now=NOW)

    # Today is the Beirut 16th: the 21:00:00 UTC order is in, the 20:59:59 one is not.
    assert p["today_orders"] == 3
    assert p["today_revenue"] == "1234.86"
    assert p["avg_invoice_value_today"] == "411.62"
    assert p["week_revenue"] == "2065.34"
    assert p["prev_week_revenue"] == "1010.01"
    assert p["chart_data"] == [
        {"date": "2026-06-10", "revenue": "310.07", "is_today": False},
        {"date": "2026-06-11", "revenue": "0.00", "is_today": False},
        {"date": "2026-06-12", "revenue": "420.42", "is_today": False},
        {"date": "2026-06-13", "revenue": "0.00", "is_today": False},
        {"date": "2026-06-14", "revenue": "0.00", "is_today": False},
        {"date": "2026-06-15", "revenue": "99.99", "is_today": False},
        {"date": "2026-06-16", "revenue": "1234.86", "is_today": True},
    ]
    assert {r["karat"]: r["grams"] for r in p["gold_weight_sold_today_by_karat"]} == {
        "K18": 6.25, "K21": 5.583, "K22": 1.111, "K24": 8.0}
    assert p["making_charges_today"] == "56.08"
    assert p["loss_prevention"] == {"order_voids": 3, "rate_overrides": 1, "excess_discount_orders": 2}
    # Aging runs on the same instant: 365 days old is not yet dead stock, 366 is.
    assert p["inventory_aging"] == {"d0_90": 2, "d90_180": 2, "d180_365": 3, "d365_plus": 3}
    assert p["dead_stock_count"] == 2
    assert p["vat_position"] == {"net_payable": "67.34", "direction": "PAYABLE", "period_label": "Q2 2026"}
    assert p["cash_bank_balance"] == "17891.06"


@pytest.mark.asyncio
async def test_every_section_runs_on_the_same_windows(shop, monkeypatch):
    clock_reads = []
    real_windows = dash.windows

    def spy_windows(now):
        clock_reads.append(now)
        return real_windows(now)

    received = []

    def spied(section):
        async def run(db, w):
            received.append(w)
            return await section(db, w)
        return run

    monkeypatch.setattr(dash, "windows", spy_windows)
    monkeypatch.setattr(reports, "_SECTIONS", tuple(spied(s) for s in reports._SECTIONS))

    await reports.build_dashboard(shop.sessions, now=NOW)

    assert clock_reads == [NOW]                       # boundaries computed exactly once
    assert len(received) == len(reports._SECTIONS)
    assert all(w is received[0] for w in received)    # and shared by every branch


# ── bounded fan-out over separate sessions ────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2, 4])
async def test_fan_out_is_bounded_and_never_shares_a_session(shop, limit):
    await reports.build_dashboard(shop.sessions, now=NOW, max_concurrency=limit)

    t = shop.tracker
    assert t.peak == limit            # the limit is reached, and never exceeded
    assert t.opened == limit          # one session per concurrent branch
    assert t.overlaps == 0            # no session ran two statements at once
    assert t.open == 0                # everything handed back to the pool


@pytest.mark.asyncio
async def test_fan_out_never_opens_more_sessions_than_sections(shop):
    await reports.build_dashboard(shop.sessions, now=NOW, max_concurrency=1000,
                                  gate=asyncio.Semaphore(1000))
    assert shop.tracker.peak == shop.tracker.opened == len(reports._SECTIONS)


def test_both_limits_are_settings_with_pool_safe_defaults():
    from app.config import Settings, settings

    # Per load: 4 lanes. Across all loads in the process: 6 sessions, well inside
    # the engine's 5 + 10 overflow, so the till always finds a free connection.
    assert (settings.dashboard_max_concurrency, settings.dashboard_max_sessions) == (4, 6)

    tuned = Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:", jwt_secret="x",
                     dashboard_max_concurrency=2, dashboard_max_sessions=3)
    assert (tuned.dashboard_max_concurrency, tuned.dashboard_max_sessions) == (2, 3)
    for bad in ({"dashboard_max_concurrency": 0}, {"dashboard_max_sessions": 0}):
        with pytest.raises(ValueError):
            Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:", jwt_secret="x", **bad)


def test_settings_accept_the_knobs_from_a_dotenv_file(tmp_path):
    """Declared fields, so a .env that sets them no longer breaks startup."""
    from app.config import Settings

    env = tmp_path / ".env"
    env.write_text("DATABASE_URL=sqlite+aiosqlite:///:memory:\nJWT_SECRET=x\n"
                   "DASHBOARD_MAX_CONCURRENCY=3\nDASHBOARD_MAX_SESSIONS=5\n")
    loaded = Settings(_env_file=str(env))
    assert (loaded.dashboard_max_concurrency, loaded.dashboard_max_sessions) == (3, 5)


@pytest.mark.asyncio
async def test_default_load_uses_the_configured_lane_count(shop, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "dashboard_max_concurrency", 2)
    await reports.build_dashboard(shop.sessions, now=NOW)
    assert shop.tracker.peak == shop.tracker.opened == 2


# ── process-wide cap: simultaneous loads queue for lanes ──────────────────────

@pytest.mark.asyncio
async def test_process_gate_is_shared_and_sized_from_settings(monkeypatch):
    from app.config import settings

    gate = dash.lane_gate()
    assert dash.lane_gate() is gate                       # one gate for every load on this loop
    for _ in range(settings.dashboard_max_sessions):      # exactly dashboard_max_sessions slots
        assert not gate.locked()
        await gate.acquire()
    assert gate.locked()
    for _ in range(settings.dashboard_max_sessions):
        gate.release()

    monkeypatch.setattr(dash, "_lane_gate", None)         # as in a fresh process
    monkeypatch.setattr(settings, "dashboard_max_sessions", 1)
    fresh = dash.lane_gate()
    await fresh.acquire()
    assert fresh.locked()
    fresh.release()


@pytest.mark.asyncio
async def test_simultaneous_loads_never_hold_more_than_the_cap(tmp_path):
    """Five loads at once, four lanes each, capped at three sessions between
    them — on a pool with exactly one connection to spare. The spare one is the
    till's: a non-dashboard request must get it at any moment."""
    cap, loads = 3, 5
    env = await _make_shop(tmp_path / "busy.db", _seed_shop,
                           pool_size=cap + 1, max_overflow=0, pool_timeout=3)
    try:
        expected = _body(await _oracle(env))
        env.tracker.reset()
        gate = asyncio.Semaphore(cap)
        elsewhere = async_sessionmaker(env.engine, expire_on_commit=False)
        finished = asyncio.Event()
        lanes_open, connections_out = [], []

        async def checkout_at_the_till():
            while not finished.is_set():
                async with elsewhere() as db:              # raises if the pool is drained
                    await db.execute(select(User.id).limit(1))
                    lanes_open.append(env.tracker.open)
                    connections_out.append(env.engine.pool.checkedout())
                await asyncio.sleep(0)

        till = asyncio.ensure_future(checkout_at_the_till())
        payloads = await asyncio.wait_for(asyncio.gather(*(
            reports.build_dashboard(env.sessions, now=NOW, max_concurrency=4, gate=gate)
            for _ in range(loads))), timeout=60)
        finished.set()
        await till

        assert [_body(p) for p in payloads] == [expected] * loads
        assert env.tracker.peak == cap                     # saturated, never exceeded
        assert env.tracker.overlaps == 0 and env.tracker.open == 0
        assert cap in lanes_open                           # the till got through at full load
        assert max(connections_out) <= cap + 1             # lanes + the till's own connection
        assert not gate.locked()                           # every slot handed back
    finally:
        await env.engine.dispose()


@pytest.mark.asyncio
async def test_lane_that_queued_past_the_work_opens_no_session(shop):
    # One slot: the first lane does everything; the others find nothing left.
    await reports.build_dashboard(shop.sessions, now=NOW, max_concurrency=4, gate=asyncio.Semaphore(1))
    assert shop.tracker.opened == 1


@pytest.mark.asyncio
async def test_failed_load_returns_its_slots(shop, monkeypatch):
    gate = asyncio.Semaphore(2)
    real = dash.loss_prevention

    async def boom(db, start, end):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(dash, "loss_prevention", boom)
    with pytest.raises(RuntimeError):
        await reports.build_dashboard(shop.sessions, now=NOW, max_concurrency=4, gate=gate)
    assert not gate.locked() and shop.tracker.open == 0

    monkeypatch.setattr(dash, "loss_prevention", real)
    await asyncio.wait_for(                                # would hang on a leaked slot
        reports.build_dashboard(shop.sessions, now=NOW, max_concurrency=4, gate=gate), timeout=30)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit, patience, expect_overlap", [(4, 5.0, True), (2, 5.0, True), (1, 0.1, False)])
async def test_sections_overlap_in_time_only_when_allowed(shop, monkeypatch, limit, patience, expect_overlap):
    """Two helpers from different sections: the first to start holds its branch
    open until the other is in flight too. They can only meet if their branches
    run at the same moment — with one lane, patience runs out instead."""
    expected = _body(await _oracle(shop))
    in_flight = set()
    overlapped = asyncio.Event()

    def rendezvous(real, me, other):
        async def helper(*args, **kwargs):
            in_flight.add(me)
            try:
                if other in in_flight:
                    overlapped.set()
                else:
                    try:
                        await asyncio.wait_for(overlapped.wait(), timeout=patience)
                    except asyncio.TimeoutError:
                        pass
                return await real(*args, **kwargs)
            finally:
                in_flight.discard(me)
        return helper

    monkeypatch.setattr(dash, "inventory_aging", rendezvous(dash.inventory_aging, "aging", "loss"))
    monkeypatch.setattr(dash, "loss_prevention", rendezvous(dash.loss_prevention, "loss", "aging"))

    actual = await reports.build_dashboard(shop.sessions, now=NOW, max_concurrency=limit)

    assert overlapped.is_set() is expect_overlap
    assert _body(actual) == expected


# ── failure and read-only guarantees ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_one_failing_section_fails_the_whole_request(shop, monkeypatch):
    async def boom(db, start, end):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(dash, "loss_prevention", boom)

    with pytest.raises(RuntimeError, match="ledger unavailable"):
        await reports.build_dashboard(shop.sessions, now=NOW)
    # No branch is left running behind the failed request: every lane has
    # already unwound and handed its session back.
    assert shop.tracker.open == 0


@pytest.mark.asyncio
async def test_http_errors_from_a_section_surface_unchanged(shop, monkeypatch):
    async def refuse(db, *, as_of):
        raise HTTPException(status_code=422, detail="quarter must be 1–4")

    monkeypatch.setattr(dash, "receivables", refuse)

    with pytest.raises(HTTPException) as exc:
        await reports.build_dashboard(shop.sessions, now=NOW)
    assert exc.value.status_code == 422
    assert shop.tracker.open == 0


@pytest.mark.asyncio
async def test_missing_gold_rate_fails_both_paths_alike(tmp_path):
    async def nothing(db):
        return None

    env = await _make_shop(tmp_path / "empty.db", nothing)
    try:
        with pytest.raises(RuntimeError, match="No gold rate available"):
            await _oracle(env)
        with pytest.raises(RuntimeError, match="No gold rate available"):
            await reports.build_dashboard(env.sessions, now=NOW)
        assert env.tracker.open == 0
    finally:
        await env.engine.dispose()


@pytest.mark.asyncio
async def test_dashboard_never_commits(shop):
    commits = []

    def on_commit(conn):
        commits.append(conn)

    event.listen(shop.engine.sync_engine, "commit", on_commit)
    try:
        await reports.build_dashboard(shop.sessions, now=NOW)
        assert commits == []

        async with shop.sessions() as db:          # the listener does see real commits
            db.add(Supplier(name="Late arrival"))
            await db.commit()
        assert len(commits) == 1
    finally:
        event.remove(shop.engine.sync_engine, "commit", on_commit)


# ── round-trips ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_round_trips_are_spread_across_sessions(shop):
    with _Statements(shop.engine) as before:
        await _oracle(shop)
    with _Statements(shop.engine) as after:
        await reports.build_dashboard(shop.sessions, now=NOW)

    # Same queries minus the three low-stock counts the old handler ran and then
    # discarded (it reported dash.low_stock_count instead).
    assert after.count == before.count - 3
    print(f"\ndashboard round-trips: sequential={before.count} on 1 session, "
          f"concurrent={after.count} across {shop.tracker.opened - 1} sessions")


# ── the endpoint ──────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client(shop):
    from app.deps import get_current_user, get_session_factory
    from app.main import app

    async def _admin():
        return User(id="u1", name="Rima", email="rima@x.co", password_hash="x",
                    role=Role.ADMIN, is_active=True)

    app.dependency_overrides[get_current_user] = _admin
    app.dependency_overrides[get_session_factory] = lambda: shop.sessions
    app.dependency_overrides[reports.utc_now] = lambda: NOW
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_endpoint_serves_the_concurrent_payload(client, shop):
    expected = _body(await _oracle(shop))
    shop.tracker.reset()

    r = await client.get("/api/reports/dashboard")

    assert r.status_code == 200
    assert r.content == expected
    assert shop.tracker.opened == 4                         # fanned out, not sequential
    assert shop.tracker.peak == 4


@pytest.mark.asyncio
async def test_simultaneous_requests_share_the_process_cap(client, shop):
    expected = _body(await _oracle(shop))
    shop.tracker.reset()

    responses = await asyncio.wait_for(
        asyncio.gather(*(client.get("/api/reports/dashboard") for _ in range(4))), timeout=60)

    assert [r.status_code for r in responses] == [200] * 4
    assert all(r.content == expected for r in responses)
    assert shop.tracker.peak == 6                          # 4 loads × 4 lanes, held to the cap
    assert shop.tracker.open == 0


@pytest.mark.asyncio
async def test_endpoint_hands_back_the_request_connection_before_fanning_out(client, shop, monkeypatch):
    """Auth runs on the request's own session, which then holds a connection
    until the response is sent. The dashboard has no further use for it, so it
    must not sit idle behind the lanes (or behind a load queued for lanes)."""
    from fastapi import Depends

    from app.deps import get_current_user, get_db
    from app.main import app

    plain = async_sessionmaker(shop.engine, expire_on_commit=False)

    async def _request_session():
        async with plain() as db:
            yield db

    async def _authenticate(db: AsyncSession = Depends(get_db)):     # as deps.get_current_user does
        return (await db.execute(select(User).where(User.id == "u1"))).scalar_one()

    out = []
    real = dash.loss_prevention

    async def spy(db, start, end):
        out.append(shop.engine.pool.checkedout())
        return await real(db, start, end)

    app.dependency_overrides[get_db] = _request_session
    app.dependency_overrides[get_current_user] = _authenticate
    monkeypatch.setattr(dash, "loss_prevention", spy)
    r = await client.get("/api/reports/dashboard")

    assert r.status_code == 200
    assert r.content == _body(await _oracle(shop))
    assert out and max(out) <= 4                           # lanes only: no fifth, idle connection


@pytest.mark.asyncio
async def test_endpoint_still_requires_admin(client, shop):
    from app.deps import get_current_user
    from app.main import app

    async def _cashier():
        return User(id="u2", name="Karim", email="karim@x.co", password_hash="x",
                    role=Role.CASHIER, is_active=True)

    app.dependency_overrides[get_current_user] = _cashier
    r = await client.get("/api/reports/dashboard")
    assert r.status_code == 403
    assert shop.tracker.opened == 0


# ── NEX-54: no monetary value is a JSON number ────────────────────────────────
# Every leaf of the payload, classified. The walk fails on any leaf that is not
# listed here, so a new field has to be declared money or not-money before it
# ships — money cannot slip back in as a number.

_MONEY_PATHS = {
    "today_revenue", "week_revenue", "prev_week_revenue", "avg_invoice_value_today",
    "gold_rate_24k", "chart_data[].revenue", "top_sellers[].revenue",
    "making_charges_today", "making_charges_week", "recent_orders[].total_usd",
    "inventory_value.total_usd", "inventory_value.pure_gold_usd", "inventory_value.coins_usd",
    "inventory_value.ounces_usd", "inventory_value.products_usd", "inventory_value.rate_24k",
    "recent_purchases[].total_cash_due",
    "receivables.total", "receivables.b0_30", "receivables.b31_60", "receivables.b61_90",
    "receivables.b90_plus",
    "payables_aging.cash_total", "payables_aging.b0_30", "payables_aging.b31_60",
    "payables_aging.b61_90", "payables_aging.b90_plus",
    "cash_bank_balance", "vat_position.net_payable",
    "profitability.gross_profit", "profitability.profit_per_gram",
}
# Numbers that are not money: counts, gram weights, and one percentage.
_NUMBER_PATHS = {
    "today_orders", "top_sellers[].units", "dead_stock_count", "recent_purchases[].item_count",
    "gold_weight_sold_today_by_karat[].grams", "gold_weight_sold_week_by_karat[].grams",
    "inventory.pure_gold_by_karat[].grams_remaining", "inventory.pure_gold_by_karat[].lot_count",
    "inventory.coins.on_hand_total", "inventory.coins.distinct_types",
    "inventory.ounces.on_hand_total", "inventory.ounces.distinct_types",
    "inventory.low_stock_alerts",
    "inventory_aging.d0_90", "inventory_aging.d90_180", "inventory_aging.d180_365",
    "inventory_aging.d365_plus",
    "payables_aging.metal_owed_by_karat.{karat}",
    "loss_prevention.order_voids", "loss_prevention.rate_overrides",
    "loss_prevention.excess_discount_orders",
    "profitability.gross_margin_pct",
}
# Text, flags and timestamps.
_OTHER_PATHS = {
    "gold_rate_is_stale", "gold_rate_fetched_at", "chart_data[].date", "chart_data[].is_today",
    "top_sellers[].code", "top_sellers[].name", "top_sellers[].karat",
    "gold_weight_sold_today_by_karat[].karat", "gold_weight_sold_week_by_karat[].karat",
    "recent_orders[].id", "recent_orders[].order_number", "recent_orders[].status",
    "recent_orders[].cashier", "recent_orders[].created_at",
    "inventory.pure_gold_by_karat[].karat", "inventory_value.method",
    "recent_purchases[].id", "recent_purchases[].supplier", "recent_purchases[].occurred_at",
    "vat_position.direction", "vat_position.period_label", "profitability.since",
}
_MONEY_STRING = re.compile(r"^-?\d+\.\d{2}$")


def _leaves(node, path=""):
    """Yield (path, value) for every scalar; list items share one `[]` path."""
    if isinstance(node, dict):
        for key, value in node.items():
            if path == "payables_aging.metal_owed_by_karat":
                key = "{karat}"
            yield from _leaves(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for value in node:
            yield from _leaves(value, f"{path}[]")
    else:
        yield path, node


def _assert_no_money_as_number(payload: dict) -> set:
    leaves = [(p, v) for p, v in _leaves(payload) if v is not None]
    for path, value in leaves:
        if path in _MONEY_PATHS:
            assert isinstance(value, str) and _MONEY_STRING.match(value), f"{path} = {value!r}"
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            assert path in _NUMBER_PATHS, f"{path} = {value!r}: a JSON number not classified as non-monetary"
        else:
            assert path in _OTHER_PATHS, f"{path} = {value!r}: unclassified leaf"
    return {p for p, _ in leaves}


@pytest.mark.asyncio
async def test_no_monetary_value_in_the_dashboard_payload_is_a_json_number(client):
    r = await client.get("/api/reports/dashboard")
    assert r.status_code == 200

    seen = _assert_no_money_as_number(r.json())
    # The rich fixture exercises every leaf, so nothing above is vacuous.
    assert seen == _MONEY_PATHS | _NUMBER_PATHS | _OTHER_PATHS
    # And on the wire the amounts are quoted, cents intact.
    assert b'"today_revenue":"1234.86"' in r.content
    assert b'"revenue":"0.00"' in r.content                      # a zero keeps its two decimals
    assert b'"cash_bank_balance":"17891.06"' in r.content


@pytest.mark.asyncio
async def test_dormant_dashboard_payload_has_no_money_as_number(dormant_shop):
    payload = await reports.build_dashboard(dormant_shop.sessions, now=NOW)
    seen = _assert_no_money_as_number(payload)
    assert payload["today_revenue"] == "0.00" and payload["receivables"]["total"] == "0.00"
    assert payload["cash_bank_balance"] is None                  # null stays null, not "0.00"
    assert "cash_bank_balance" not in seen


def test_production_session_source_is_the_app_session_factory():
    from app.db.session import async_session_factory
    from app.deps import get_session_factory

    assert get_session_factory() is async_session_factory
    assert reports.utc_now().tzinfo is UTC
