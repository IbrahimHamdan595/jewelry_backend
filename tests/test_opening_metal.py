from datetime import date
from decimal import Decimal

import pytest

from app.core import gl
from app.core.coa_seed import seed_chart_of_accounts, post_opening_balances
from app.models import GLPeriod, PeriodStatus, CoinType, MarginMode, Karat

D = Decimal


@pytest.mark.asyncio
async def test_opening_loads_finished_goods_metal(db):
    await seed_chart_of_accounts(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    # A coin in stock: 5 units @ 8g K21, no tracked cost.
    db.add(CoinType(code="C1", name_en="Coin", karat=Karat.K21, weight_grams=D("8"),
                    margin_mode=MarginMode.USD, margin_value=D("5"), on_hand_qty=5))
    await db.flush()
    await post_opening_balances(db, as_of=date(2026, 6, 1), actor_user_id="u1",
                                cash_lines=[], gold_rate_24k=D("60"))
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 1))
    assert tb["balanced"] and tb["metal_balanced"]
    accts = {a["system_key"]: a for a in tb["accounts"]}
    # 5 coins × 8g = 40g K21 in inventory.
    assert accts["METAL_INVENTORY"]["metal_by_karat"]["K21"]["net_grams"] == D("40.000")
