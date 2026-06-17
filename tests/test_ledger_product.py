import pytest, pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select


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
async def test_product_crud_writes_ledger(client, db):
    from app.models import InventoryLedger
    r = await client.post("/api/products", json={
        "name_en": "Ring", "name_ar": "", "category": "Rings", "karat": "K18",
        "weight_grams": "5", "margin_percent": "15", "making_charge": "20",
    })
    assert r.status_code in (200, 201), r.text
    pid = r.json()["id"]
    await client.patch(f"/api/products/{pid}", json={"making_charge": "30"})
    await client.delete(f"/api/products/{pid}")
    rows = (await db.execute(
        select(InventoryLedger).where(InventoryLedger.ref_type == "product").order_by(InventoryLedger.occurred_at)
    )).scalars().all()
    assert [r.event_type for r in rows] == ["PRODUCT_CREATED", "PRODUCT_UPDATED", "PRODUCT_DELETED"]
    assert rows[0].payload.get("code")  # generated code captured
