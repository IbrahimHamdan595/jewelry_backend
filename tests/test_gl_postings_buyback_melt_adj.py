from datetime import date
from decimal import Decimal

import pytest

from app.core import gl
from app.core import gl_postings as glp
from app.core.coa_seed import seed_chart_of_accounts
from app.models import GLPeriod, PeriodStatus, Settings, Karat

D = Decimal


async def _seeded(db):
    await seed_chart_of_accounts(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    await db.flush()


def _settings(on=True):
    return Settings(id="singleton", accounting_auto_post_enabled=on)


class _Buyback:
    def __init__(self):
        self.id = "bb1"; self.buy_price_usd = D("500"); self.weight_grams = D("10.000")
        self.karat = Karat.K21; self.quantity = 1; self.occurred_at = None


@pytest.mark.asyncio
async def test_buyback_balances_via_clearing(db):
    await _seeded(db)
    entry = await glp.post_buyback(db, _Buyback(), _settings(), "u1")
    assert entry is not None
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    assert tb["balanced"] and tb["metal_balanced"]
    accts = {a["system_key"]: a for a in tb["accounts"]}
    assert accts["METAL_INVENTORY"]["metal_by_karat"]["K21"]["net_grams"] == D("10.000")
    assert accts["METAL_CLEARING"]["metal_by_karat"]["K21"]["net_grams"] == D("-10.000")
    assert accts["CASH"]["base_credit"] == D("500.00")


class _Melt:
    def __init__(self):
        self.id = "m1"; self.occurred_at = None
        self.from_karat = Karat.K21; self.from_grams = D("20.000")
        self.to_karat = Karat.K24; self.to_grams = D("17.000"); self.cost_usd = D("1200")


@pytest.mark.asyncio
async def test_melt_balances_each_karat_via_clearing(db):
    await _seeded(db)
    entry = await glp.post_melt(db, _Melt(), _settings(), "u1")
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    assert tb["metal_balanced"]
    accts = {a["system_key"]: a for a in tb["accounts"]}
    assert accts["METAL_INVENTORY"]["metal_by_karat"]["K21"]["net_grams"] == D("-20.000")
    assert accts["METAL_INVENTORY"]["metal_by_karat"]["K24"]["net_grams"] == D("17.000")


class _Adjustment:
    def __init__(self):
        self.id = "a1"; self.occurred_at = None
        self.karat = Karat.K21; self.grams = D("5.000"); self.cost_usd = D("300")


@pytest.mark.asyncio
async def test_adjustment_loss_balances(db):
    await _seeded(db)
    entry = await glp.post_adjustment(db, _Adjustment(), _settings(), "u1")
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    assert tb["balanced"] and tb["metal_balanced"]
    accts = {a["system_key"]: a for a in tb["accounts"]}
    assert accts["ADJUSTMENT_EXPENSE"]["base_debit"] == D("300.00")
    assert accts["METAL_INVENTORY"]["metal_by_karat"]["K21"]["net_grams"] == D("-5.000")
    assert accts["METAL_CLEARING"]["metal_by_karat"]["K21"]["net_grams"] == D("5.000")
