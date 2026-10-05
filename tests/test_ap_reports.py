from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest

from app.core import gl_postings, ap
from app.core.coa_seed import seed_chart_of_accounts
from app.core.supplier_balance import adjust_balance
from app.models import (
    GLPeriod, PeriodStatus, Settings, Supplier, SupplierPurchase, SupplierPurchaseItem,
    SupplierPurchaseMode, SupplierItemKind, SupplierPayment, DebtUnit, Karat,
)


async def _seed(db):
    await seed_chart_of_accounts(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    await db.flush()


def _settings(on=True):
    return Settings(id="singleton", accounting_auto_post_enabled=on)


@pytest.mark.asyncio
async def test_verify_ap_control_matches(db):
    await _seed(db)
    sup = Supplier(name="ACME"); db.add(sup); await db.flush()
    pur = SupplierPurchase(supplier_id=sup.id, payment_mode=SupplierPurchaseMode.MIXED,
                           total_cash_due=D("700"), total_grams_due_by_karat={"K21": "30.000"},
                           cash_paid_at_creation=D("0"), grams_paid_at_creation_by_karat={},
                           created_by_user_id="u1")
    pur.items = [SupplierPurchaseItem(item_kind=SupplierItemKind.PRODUCT, unit_cost_usd=D("700")),
                 SupplierPurchaseItem(item_kind=SupplierItemKind.PURE_GOLD, karat=Karat.K21,
                                      weight_grams=D("30.000"), unit_cost_usd=D("1800"))]
    db.add(pur); await db.flush()
    await gl_postings.post_supplier_purchase(db, pur, _settings(), "u1")
    await adjust_balance(db, supplier_id=sup.id, unit=DebtUnit.CASH, karat="", delta=D("700"))
    await adjust_balance(db, supplier_id=sup.id, unit=DebtUnit.GOLD, karat="K21", delta=D("30"))

    v = await ap.verify_ap_control(db)
    assert v["ap"]["gl"] == D("700.00") and v["ap"]["matches"]
    assert v["metal_ap"]["by_karat"]["K21"]["matches"] is True
    assert v["metal_ap"]["matches"] is True


@pytest.mark.asyncio
async def test_ap_aging_fifo_buckets(db):
    await _seed(db)
    sup = Supplier(name="ACME"); db.add(sup); await db.flush()
    p1 = SupplierPurchase(supplier_id=sup.id, payment_mode=SupplierPurchaseMode.CASH,
                          total_cash_due=D("100"), total_grams_due_by_karat={},
                          cash_paid_at_creation=D("0"), grams_paid_at_creation_by_karat={},
                          created_by_user_id="u1")
    p1.occurred_at = datetime(2026, 4, 26, tzinfo=timezone.utc)
    p2 = SupplierPurchase(supplier_id=sup.id, payment_mode=SupplierPurchaseMode.CASH,
                          total_cash_due=D("50"), total_grams_due_by_karat={},
                          cash_paid_at_creation=D("0"), grams_paid_at_creation_by_karat={},
                          created_by_user_id="u1")
    p2.occurred_at = datetime(2026, 5, 31, tzinfo=timezone.utc)
    db.add_all([p1, p2])
    await db.flush()
    db.add(SupplierPayment(supplier_id=sup.id, unit=DebtUnit.CASH, karat=None, amount=D("30"),
                           paid_by_user_id="u1"))
    await db.flush()
    aging = await ap.compute_ap_aging(db, as_of=date(2026, 6, 5))
    assert aging["cash_buckets"]["31_60"] == D("70.00")  # p1 remaining after FIFO 30
    assert aging["cash_buckets"]["0_30"] == D("50.00")   # p2 untouched
    assert aging["cash_total"] == D("120.00")


@pytest.mark.asyncio
async def test_supplier_statement_running_balance(db):
    await _seed(db)
    sup = Supplier(name="ACME"); db.add(sup); await db.flush()
    p = SupplierPurchase(supplier_id=sup.id, payment_mode=SupplierPurchaseMode.CASH,
                         total_cash_due=D("100"), total_grams_due_by_karat={},
                         cash_paid_at_creation=D("0"), grams_paid_at_creation_by_karat={},
                         created_by_user_id="u1")
    p.occurred_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    db.add(p)
    pay = SupplierPayment(supplier_id=sup.id, unit=DebtUnit.CASH, karat=None, amount=D("40"),
                          paid_by_user_id="u1")
    pay.paid_at = datetime(2026, 6, 3, tzinfo=timezone.utc)
    db.add(pay)
    await db.flush()
    st = await ap.supplier_statement(db, sup.id, from_date=date(2026, 6, 1), until=date(2026, 6, 30))
    assert st["closing_cash_balance"] == D("60.00")
    assert len(st["events"]) == 2


import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client(db):
    from app.main import app
    from app.deps import get_db, get_current_user
    from app.models import User, Role
    admin = User(id="u-admin", email="a@x.com", name="A", password_hash="x", role=Role.ADMIN, is_active=True)
    db.add(admin)
    await _seed(db)

    async def _get_db():
        yield db

    async def _get_user():
        return admin

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = _get_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_ap_api_verify_and_aging(client):
    v = (await client.get("/api/accounting/ap/verify")).json()
    assert v["ap"]["matches"] is True  # zero == zero
    a = (await client.get("/api/accounting/ap/aging?as_of=2026-06-30")).json()
    assert a["cash_total"] == "0.00"


# ── AP aging in grouped queries vs the per-supplier oracle (NEX-53) ───────────
import json
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import event

from app.models import SupplierBalance
from tests.ap_aging_oracle import compute_ap_aging_per_supplier

_AS_OF = date(2026, 6, 30)


def _days_ago(n: int) -> datetime:
    return datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc) - timedelta(days=n)


async def _seed_many_suppliers(db):
    """Eight suppliers covering every branch of the aging: all four buckets and
    their edges, FIFO across several payments, over- and under-payment, cash
    paid at creation, gold in several karats, and suppliers with nothing."""
    def purchase(sup, days, due, paid="0"):
        return SupplierPurchase(supplier_id=sup.id, occurred_at=_days_ago(days),
                                payment_mode=SupplierPurchaseMode.CASH, total_cash_due=D(due),
                                total_grams_due_by_karat={}, cash_paid_at_creation=D(paid),
                                grams_paid_at_creation_by_karat={}, created_by_user_id="u1")

    def cash(sup, amount, days=1):
        return SupplierPayment(supplier_id=sup.id, unit=DebtUnit.CASH, amount=D(amount),
                               paid_by_user_id="u1", paid_at=_days_ago(days))

    def gold(sup, karat, grams):
        return SupplierBalance(supplier_id=sup.id, unit=DebtUnit.GOLD, karat=karat, balance=D(grams))

    names = ("Bullion", "Sidon", "Idle", "Settled", "Overpaid", "Edges", "Same day", "Metal only")
    sups = {n: Supplier(name=n) for n in names}
    db.add_all(sups.values())
    await db.flush()
    s = sups
    db.add_all([
        # Bullion: two payments (900) clear the oldest 800 and 100 of the next.
        purchase(s["Bullion"], 5, "150.00"),                       # inserted newest-first on purpose
        purchase(s["Bullion"], 40, "300.25", paid="0.25"),
        purchase(s["Bullion"], 70, "500.50"),
        purchase(s["Bullion"], 120, "1000.00", paid="200.00"),
        cash(s["Bullion"], "600.00", days=60), cash(s["Bullion"], "300.00", days=30),
        SupplierPayment(supplier_id=s["Bullion"].id, unit=DebtUnit.GOLD, karat=Karat.K21,
                        amount=D("5.000"), paid_by_user_id="u1", paid_at=_days_ago(3)),
        SupplierBalance(supplier_id=s["Bullion"].id, unit=DebtUnit.CASH, karat="", balance=D("850.50")),
        gold(s["Bullion"], "K21", "30.500"), gold(s["Bullion"], "K18", "0.000"),
        gold(s["Bullion"], "K24", "0.250"),
        # Sidon: one old unpaid purchase, gold in two karats (K21 shared with Bullion).
        purchase(s["Sidon"], 95, "2000.00"),
        gold(s["Sidon"], "K21", "12.345"), gold(s["Sidon"], "K22", "3.000"),
        # Settled: paid in full at creation, plus a stray payment with nothing to apply to.
        purchase(s["Settled"], 20, "60.00", paid="60.00"), cash(s["Settled"], "10.00"),
        # Overpaid: payments exceed everything outstanding.
        purchase(s["Overpaid"], 10, "100.00"), purchase(s["Overpaid"], 3, "50.00"),
        cash(s["Overpaid"], "500.00"),
        # Edges: one purchase on each side of every bucket boundary; 5 paid off the oldest.
        *[purchase(s["Edges"], d, "10.00") for d in (30, 91, 60, 31, 90, 61)],
        cash(s["Edges"], "5.00"),
        # Same day: two purchases at the same instant, payment spans both.
        purchase(s["Same day"], 15, "7.00"), purchase(s["Same day"], 15, "3.00"),
        cash(s["Same day"], "8.00"),
        # A gold-mode purchase with no cash due never ages.
        purchase(s["Metal only"], 50, "0.00"),
        gold(s["Metal only"], "K18", "-0.500"), gold(s["Metal only"], "K24", "1.001"),
    ])
    await db.flush()
    return sups


def _canon_aging(aging: dict) -> str:
    """Order-insensitive, type-strict: every amount must be an exact Decimal."""
    def enc(v):
        if isinstance(v, Decimal):
            return f"Decimal:{v}"
        if isinstance(v, date):
            return v.isoformat()
        raise TypeError(f"unexpected {type(v).__name__} in aging: {v!r}")
    return json.dumps(aging, default=enc, sort_keys=True)


async def _count_statements(db, fn):
    statements = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.bind.sync_engine, "before_cursor_execute", _capture)
    try:
        result = await fn()
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", _capture)
    return result, len(statements)


@pytest.mark.asyncio
async def test_ap_aging_grouped_queries_match_per_supplier_oracle(db):
    sups = await _seed_many_suppliers(db)

    expected, old_statements = await _count_statements(
        db, lambda: compute_ap_aging_per_supplier(db, as_of=_AS_OF))
    actual, new_statements = await _count_statements(
        db, lambda: ap.compute_ap_aging(db, as_of=_AS_OF))

    assert _canon_aging(actual) == _canon_aging(expected)
    assert list(actual["by_supplier"]) == list(expected["by_supplier"])     # same supplier order
    assert len(actual["by_supplier"]) == 8                                  # idle suppliers still listed

    # The fixture reaches every bucket, so the comparison is not vacuous.
    assert actual["cash_buckets"] == {
        "0_30": D("162.00"), "31_60": D("320.00"), "61_90": D("420.50"), "90_plus": D("2005.00")}
    assert actual["cash_total"] == D("2907.50")
    assert actual["metal_owed_by_karat"] == {
        "K21": D("42.845"), "K24": D("1.251"), "K22": D("3.000"), "K18": D("-0.500")}
    bullion = actual["by_supplier"][sups["Bullion"].id]
    assert bullion["cash_buckets"] == {
        "0_30": D("150.00"), "31_60": D("300.00"), "61_90": D("400.50"), "90_plus": D("0.00")}
    assert bullion["metal_by_karat"] == {"K21": D("30.500"), "K24": D("0.250")}
    assert actual["by_supplier"][sups["Same day"].id]["cash_buckets"]["0_30"] == D("2.00")

    # One round-trip per supplier × 3 before; a fixed four now.
    assert old_statements == 1 + 3 * 8
    assert new_statements == 4


@pytest.mark.asyncio
async def test_ap_aging_statement_count_does_not_grow_with_suppliers(db):
    await _seed_many_suppliers(db)
    db.add_all([Supplier(name=f"Extra {i}") for i in range(20)])
    await db.flush()

    actual, statements = await _count_statements(db, lambda: ap.compute_ap_aging(db, as_of=_AS_OF))
    assert statements == 4
    assert len(actual["by_supplier"]) == 28
    assert _canon_aging(actual) == _canon_aging(await compute_ap_aging_per_supplier(db, as_of=_AS_OF))


@pytest.mark.asyncio
async def test_ap_aging_with_no_suppliers_matches_oracle(db):
    actual = await ap.compute_ap_aging(db, as_of=_AS_OF)
    assert _canon_aging(actual) == _canon_aging(await compute_ap_aging_per_supplier(db, as_of=_AS_OF))
    assert actual["cash_total"] == D("0.00") and actual["by_supplier"] == {}
