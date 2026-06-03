import pytest
from sqlalchemy import select

from app.core.coa_seed import seed_chart_of_accounts
from app.models import GLAccount, Denomination, Settings


@pytest.mark.asyncio
async def test_new_system_accounts_seeded(db):
    await seed_chart_of_accounts(db)
    keys = {a.system_key for a in (await db.execute(select(GLAccount))).scalars().all()}
    assert "METAL_CLEARING" in keys
    assert "ADJUSTMENT_EXPENSE" in keys
    clearing = (await db.execute(select(GLAccount).where(GLAccount.system_key == "METAL_CLEARING"))).scalar_one()
    assert clearing.denomination == Denomination.DUAL


@pytest.mark.asyncio
async def test_settings_auto_post_flag_defaults_false(db):
    # Python-side default applies on flush, not on transient construction.
    s = Settings(id="singleton")
    db.add(s)
    await db.flush()
    assert s.accounting_auto_post_enabled is False


from datetime import date

from app.core import gl_postings as glp
from app.models import GLPeriod, PeriodStatus


@pytest.mark.asyncio
async def test_ensure_period_creates_open_then_reuses(db):
    p1 = await glp.ensure_period(db, date(2026, 6, 10))
    assert p1.status == PeriodStatus.OPEN and p1.year == 2026 and p1.period_no == 6
    p2 = await glp.ensure_period(db, date(2026, 6, 20))
    assert p2.id == p1.id  # same month reused


@pytest.mark.asyncio
async def test_resolve_account_id(db):
    await seed_chart_of_accounts(db)
    aid = await glp.resolve_account_id(db, "CASH")
    assert aid
    with pytest.raises(Exception):
        await glp.resolve_account_id(db, "NOPE")


def test_auto_post_enabled_reads_flag():
    assert glp.auto_post_enabled(Settings(id="singleton")) is False
    assert glp.auto_post_enabled(Settings(id="singleton", accounting_auto_post_enabled=True)) is True
