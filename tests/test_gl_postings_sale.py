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
