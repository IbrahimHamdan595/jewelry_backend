import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.coa_seed import seed_chart_of_accounts
from app.models import GLPeriod, PeriodStatus, Settings, User, Role


@pytest_asyncio.fixture
async def client(db):
    from app.main import app
    from app.deps import get_db, get_current_user
    admin = User(id="u-admin", email="a@x.com", name="A", password_hash="x", role=Role.ADMIN, is_active=True)
    db.add(admin)
    db.add(Settings(id="singleton", accounting_auto_post_enabled=True, vat_percent=__import__("decimal").Decimal("11")))
    await seed_chart_of_accounts(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    await db.flush()

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
async def test_customer_invoice_receipt_flow(client):
    cust = (await client.post("/api/accounting/ar/customers", json={"name": "Acme", "credit_limit": "1000"})).json()
    inv = (await client.post("/api/accounting/ar/invoices", json={
        "customer_id": cust["id"], "invoice_date": "2026-06-03", "vat_percent": "11",
        "lines": [{"description": "svc", "quantity": 1, "unit_price": "200"}]})).json()
    assert inv["total"] == "222.00"
    r = await client.post("/api/accounting/ar/receipts", json={
        "customer_id": cust["id"], "receipt_date": "2026-06-05", "amount": "222", "payment_system_key": "CASH"})
    assert r.status_code == 200, r.text
    v = (await client.get("/api/accounting/ar/verify")).json()
    assert v["matches"] is True  # control == subledger; invoice fully paid → 0


@pytest.mark.asyncio
async def test_credit_order_creates_invoice_and_ar(client, db):
    from decimal import Decimal as D
    from sqlalchemy import select
    from app.models import CoinType, MarginMode, Karat, GoldRateHistory, ARInvoice
    cust = (await client.post("/api/accounting/ar/customers", json={"name": "Bob"})).json()
    coin = CoinType(code="C9", name_en="Coin", karat=Karat.K21, weight_grams=D("10"),
                    margin_mode=MarginMode.USD, margin_value=D("5"), on_hand_qty=5)
    db.add(coin)
    db.add(GoldRateHistory(rate_24k=D("60"), source="t"))
    await db.flush()
    r = await client.post("/api/orders", json={
        "payment_method": "CREDIT", "customer_id": cust["id"],
        "items": [{"item_kind": "COIN", "coin_type_id": coin.id, "quantity": 1}]})
    assert r.status_code in (200, 201), r.text
    inv = (await db.execute(select(ARInvoice).where(ARInvoice.customer_id == cust["id"]))).scalars().first()
    assert inv is not None and inv.order_id is not None
    v = (await client.get("/api/accounting/ar/verify")).json()
    assert v["matches"] is True
