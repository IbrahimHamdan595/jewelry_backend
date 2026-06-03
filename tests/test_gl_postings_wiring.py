import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.coa_seed import seed_chart_of_accounts
from app.models import GLJournalEntry, GLPeriod, PeriodStatus, Settings, User, Role


@pytest_asyncio.fixture
async def client(db):
    from app.main import app
    from app.deps import get_db, get_current_user
    admin = User(id="u-admin", email="a@x.com", name="A", password_hash="x", role=Role.ADMIN, is_active=True)
    db.add(admin)
    db.add(Settings(id="singleton", accounting_auto_post_enabled=False))
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
async def test_sale_with_flag_off_posts_no_gl_entry(client, db):
    from decimal import Decimal as D
    from app.models import CoinType, MarginMode, Karat, GoldRateHistory
    coin = CoinType(code="C1", name_en="Coin", karat=Karat.K21, weight_grams=D("10"),
                    margin_mode=MarginMode.USD, margin_value=D("5"), on_hand_qty=5)
    db.add(coin)
    db.add(GoldRateHistory(rate_24k=D("60"), source="test"))
    await db.flush()
    r = await client.post("/api/orders", json={
        "payment_method": "CASH",
        "items": [{"item_kind": "COIN", "coin_type_id": coin.id, "quantity": 1}],
    })
    assert r.status_code in (201, 200), r.text
    # flag OFF → no GL entry
    assert (await db.execute(select(GLJournalEntry))).scalars().first() is None


@pytest.mark.asyncio
async def test_sale_with_flag_on_posts_balanced_entry(client, db):
    from decimal import Decimal as D
    from app.core import gl
    from app.models import CoinType, MarginMode, Karat, GoldRateHistory
    s = (await db.execute(select(Settings).where(Settings.id == "singleton"))).scalar_one()
    s.accounting_auto_post_enabled = True
    coin = CoinType(code="C2", name_en="Coin", karat=Karat.K21, weight_grams=D("10"),
                    margin_mode=MarginMode.USD, margin_value=D("5"), on_hand_qty=5)
    db.add(coin)
    db.add(GoldRateHistory(rate_24k=D("60"), source="test"))
    await db.flush()
    r = await client.post("/api/orders", json={
        "payment_method": "CASH",
        "items": [{"item_kind": "COIN", "coin_type_id": coin.id, "quantity": 1}],
    })
    assert r.status_code in (201, 200), r.text
    entry = (await db.execute(select(GLJournalEntry).where(GLJournalEntry.source_type == "ORDER"))).scalars().first()
    assert entry is not None
    from datetime import date
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    assert tb["balanced"] and tb["metal_balanced"]
