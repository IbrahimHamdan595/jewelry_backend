from datetime import date
from decimal import Decimal as D

import pytest

from app.core import expenses
from app.core.coa_seed import seed_chart_of_accounts
from app.models import GLPeriod, PeriodStatus, Settings


async def _seed(db):
    await seed_chart_of_accounts(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    await db.flush()


def _settings(on=True):
    return Settings(id="singleton", accounting_auto_post_enabled=on)


@pytest.mark.asyncio
async def test_expense_by_category_and_vendor_spend(db):
    await _seed(db)
    await expenses.post_vendor_bill(db, vendor_name="Landlord", supplier_id=None, bill_date=date(2026, 6, 3),
        due_date=None, lines=[{"description": "rent", "expense_system_key": "RENT_EXPENSE", "amount": D("1500")}],
        payment_system_key=None, memo="", settings=_settings(True), actor_user_id="u1")
    await expenses.post_vendor_bill(db, vendor_name="Electric Co", supplier_id=None, bill_date=date(2026, 6, 4),
        due_date=None, lines=[{"description": "power", "expense_system_key": "UTILITIES_EXPENSE", "amount": D("300")}],
        payment_system_key="CASH", memo="", settings=_settings(True), actor_user_id="u1")

    cat = await expenses.expense_by_category(db, from_date=date(2026, 6, 1), until=date(2026, 6, 30))
    by_key = {a["system_key"]: a["amount"] for a in cat["accounts"]}
    assert by_key["RENT_EXPENSE"] == D("1500.00")
    assert by_key["UTILITIES_EXPENSE"] == D("300.00")
    assert cat["total"] == D("1800.00")

    spend = await expenses.vendor_spend(db, from_date=date(2026, 6, 1), until=date(2026, 6, 30))
    by_vendor = {v["vendor_name"]: v["total"] for v in spend["vendors"]}
    assert by_vendor["Landlord"] == D("1500.00") and by_vendor["Electric Co"] == D("300.00")


import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client(db):
    from app.main import app
    from app.deps import get_db, get_current_user
    from app.models import User, Role
    admin = User(id="u-admin", email="a@x.com", name="A", password_hash="x", role=Role.ADMIN, is_active=True)
    db.add(admin)
    db.add(Settings(id="singleton", accounting_auto_post_enabled=True))
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
async def test_expenses_api_bill_and_verify(client):
    r = await client.post("/api/accounting/expenses/bills", json={
        "vendor_name": "Landlord", "bill_date": "2026-06-03",
        "lines": [{"description": "rent", "expense_system_key": "RENT_EXPENSE", "amount": "1500"}]})
    assert r.status_code == 200, r.text
    v = (await client.get("/api/accounting/expenses/verify")).json()
    assert v["matches"] is True and v["gl"] == "1500.00"
