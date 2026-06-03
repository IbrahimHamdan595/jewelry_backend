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
