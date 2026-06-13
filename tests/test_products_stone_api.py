import pytest, pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client(db):
    from app.main import app
    from app.deps import get_db, get_current_user
    from app.models import User, Role, Settings
    admin = User(id="u-admin", email="a@x.com", name="A", password_hash="x", role=Role.ADMIN, is_active=True)
    db.add(admin); db.add(Settings(id="singleton"))
    await db.flush()
    async def _get_db():
        yield db
    async def _get_user():
        return admin
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = _get_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_product_with_stones_persists_and_returns(client):
    r = await client.post("/api/products", json={
        "name_en": "Diamond Ring", "name_ar": "خاتم", "category": "Rings", "karat": "K18",
        "weight_grams": "5.0", "margin_percent": "15", "making_charge": "30",
        "stone_value_usd": "800", "stone_cost_usd": "500",
        "stone_carats": "0.5", "stone_count": 1, "stone_cert": "GIA-123",
    })
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert body["stone_value_usd"] == "800.00"
    assert body["stone_cost_usd"] == "500.00"
    assert body["stone_cert"] == "GIA-123"


@pytest.mark.asyncio
async def test_create_product_without_stones_has_null_stone_fields(client):
    r = await client.post("/api/products", json={
        "name_en": "Plain Band", "name_ar": "حلقة", "category": "Rings", "karat": "K21",
        "weight_grams": "4.0", "margin_percent": "12", "making_charge": "20",
    })
    assert r.status_code in (200, 201), r.text
    assert r.json()["stone_value_usd"] is None
