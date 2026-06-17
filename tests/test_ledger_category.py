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
async def test_category_crud_writes_ledger(client, db):
    from app.models import InventoryLedger
    r = await client.post("/api/categories", json={"name_en": "Rings", "name_ar": "", "slug": "rings"})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    await client.patch(f"/api/categories/{cid}", json={"name_en": "Fine Rings"})
    await client.delete(f"/api/categories/{cid}")
    rows = (await db.execute(
        select(InventoryLedger).where(InventoryLedger.ref_type == "category").order_by(InventoryLedger.occurred_at)
    )).scalars().all()
    assert [r.event_type for r in rows] == ["CATEGORY_CREATED", "CATEGORY_UPDATED", "CATEGORY_DELETED"]
    assert all(r.actor_user_id == "u-admin" for r in rows)
    assert all(r.ref_id == cid for r in rows)
