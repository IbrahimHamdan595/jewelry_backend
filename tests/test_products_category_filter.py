import pytest, pytest_asyncio
from decimal import Decimal as D
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client(db):
    from app.main import app
    from app.deps import get_db, get_current_user
    from app.models import User, Role
    admin = User(id="u-admin", email="a@x.com", name="A", password_hash="x", role=Role.ADMIN, is_active=True)
    db.add(admin); await db.flush()
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
async def test_filter_by_category_id(client, db):
    from app.models import Product, Category, Karat, ProductStatus
    db.add_all([
        Category(id="c1", name_en="Rings", name_ar="", slug="rings"),
        Category(id="c2", name_en="Coins", name_ar="", slug="coins-cat"),
    ])
    for i, cid in enumerate(["c1", "c1", "c2"]):
        db.add(Product(code=f"P{i}", name_en="x", name_ar="", category="x", category_id=cid,
                       karat=Karat.K18, weight_grams=D("1"), margin_percent=D("1"), making_charge=D("1"),
                       status=ProductStatus.AVAILABLE))
    await db.flush()
    r = await client.get("/api/products?category_id=c1")
    assert r.status_code == 200
    assert r.json()["total"] == 2
    assert (await client.get("/api/products?category_id=c2")).json()["total"] == 1
