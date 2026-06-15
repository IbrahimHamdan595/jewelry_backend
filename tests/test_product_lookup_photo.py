import pytest, pytest_asyncio
from decimal import Decimal as D
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client(db):
    from app.main import app
    from app.deps import get_db, get_current_user
    from app.models import User, Role, Settings, GoldRateHistory
    admin = User(id="u-admin", email="a@x.com", name="A", password_hash="x", role=Role.ADMIN, is_active=True)
    db.add(admin); db.add(Settings(id="singleton"))
    db.add(GoldRateHistory(rate_24k=D("60"), source="test"))
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


async def _add_product(db, code, photos):
    from app.models import Product, Karat, ProductStatus
    p = Product(code=code, name_en="Ring", name_ar="", category="Rings", karat=Karat.K18,
                weight_grams=D("5"), margin_percent=D("15"), making_charge=D("20"),
                on_hand_qty=3, is_active=True, status=ProductStatus.AVAILABLE, photos=photos)
    db.add(p); await db.flush()
    return p


@pytest.mark.asyncio
async def test_lookup_returns_hero_photo_url(client, db):
    await _add_product(db, "FN-K18-7001", [
        {"url": "https://cdn/x/a.jpg", "isHero": False, "order": 0},
        {"url": "https://cdn/x/hero.jpg", "isHero": True, "order": 1},
    ])
    r = await client.get("/api/products/lookup/FN-K18-7001")
    assert r.status_code == 200, r.text
    assert r.json()["photo_url"] == "https://cdn/x/hero.jpg"


@pytest.mark.asyncio
async def test_lookup_falls_back_to_first_photo_then_null(client, db):
    await _add_product(db, "FN-K18-7002", [{"url": "https://cdn/x/only.jpg", "order": 0}])
    r1 = await client.get("/api/products/lookup/FN-K18-7002")
    assert r1.json()["photo_url"] == "https://cdn/x/only.jpg"
    await _add_product(db, "FN-K18-7003", [])
    r2 = await client.get("/api/products/lookup/FN-K18-7003")
    assert r2.json()["photo_url"] is None
