"""Phase 6 — market-closed (feed staleness) flag + per-karat rate history.
Checkpoints 6.1, 6.3, 6.4."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.api.gold_price import history
from app.core.gold_api import build_rate_history_row, get_current_gold_rate
from app.models import GoldRateHistory


def test_6_3_build_row_populates_all_karats():
    row = build_rate_history_row(Decimal("100"), "goldapi")
    assert row.rate_24k == Decimal("100")
    assert row.rate_22k == Decimal("91.70")
    assert row.rate_21k == Decimal("87.50")
    assert row.rate_18k == Decimal("75.00")
    assert row.per_karat_backfilled is False  # live point, not backfill


@pytest.mark.asyncio
async def test_6_1_market_closed_when_feed_stale(db):
    # A rate fetched 2h ago, no override → stale beyond the 45-min closed
    # threshold → market_closed True.
    old = GoldRateHistory(
        id="old", rate_24k=Decimal("100"), source="live",
        fetched_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    db.add(old)
    await db.commit()
    info = await get_current_gold_rate(db)
    assert info["is_stale"] is True
    assert info["market_closed"] is True


@pytest.mark.asyncio
async def test_6_1b_market_open_when_fresh(db):
    fresh = GoldRateHistory(
        id="fresh", rate_24k=Decimal("100"), source="live",
        fetched_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    db.add(fresh)
    await db.commit()
    info = await get_current_gold_rate(db)
    assert info["is_stale"] is False
    assert info["market_closed"] is False


@pytest.mark.asyncio
async def test_6_4_history_returns_stored_per_karat(db):
    row = build_rate_history_row(Decimal("80"), "live")
    row.id = "p1"
    row.fetched_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.add(row)
    await db.commit()

    points = await history(range="24h", db=db)
    assert len(points) == 1
    p = points[0]
    assert p.rate_24k == 80.0
    assert p.rate_22k == 73.36  # 80 * 0.917
    assert p.rate_21k == 70.0   # 80 * 0.875
    assert p.rate_18k == 60.0   # 80 * 0.750
    assert p.per_karat_backfilled is False


@pytest.mark.asyncio
async def test_6_4b_history_derives_for_legacy_null_rows(db):
    # A row predating the backfill (per-karat NULL) still yields a derived point.
    legacy = GoldRateHistory(
        id="legacy", rate_24k=Decimal("100"), source="live",
        fetched_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    db.add(legacy)
    await db.commit()
    points = await history(range="24h", db=db)
    p = next(x for x in points if x.rate_24k == 100.0)
    assert p.rate_21k == 87.5  # derived fallback


# ── Shop-tuned thresholds (GOLD_REFRESH_MINUTES=10, FAILURE_THRESHOLD=2) ──────
# Warn at 10 min, require sale acknowledgement at 20 min. These pin the
# derivation in gold_api.py so a future config change cannot silently move the
# point at which the till starts demanding a confirmation.

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "age_minutes,expect_stale,expect_closed",
    [
        (9, False, False),   # fresh
        (11, True, False),   # warn only — amber strip, no friction
        (21, True, True),    # acknowledgement required
    ],
)
async def test_shop_tuned_staleness_boundaries(
    db, monkeypatch, age_minutes, expect_stale, expect_closed
):
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "gold_refresh_minutes", 10)
    monkeypatch.setattr(app_settings, "gold_alert_failure_threshold", 2)

    row = GoldRateHistory(
        id=f"age-{age_minutes}", rate_24k=Decimal("100"), source="live",
        fetched_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
    )
    db.add(row)
    await db.commit()

    info = await get_current_gold_rate(db)
    assert info["is_stale"] is expect_stale
    assert info["market_closed"] is expect_closed
