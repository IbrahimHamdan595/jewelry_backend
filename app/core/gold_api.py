import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import GoldRateHistory, GoldRateOverride

log = logging.getLogger(__name__)


@dataclass
class GoldRateResult:
    value: Decimal
    source: str


# Purity multipliers used to derive per-karat values from the 24K feed price.
# Mirrors app.core.pricing.KARAT_PURITY (kept inline to avoid a circular import).
_PURITY_22K = Decimal("0.917")
_PURITY_21K = Decimal("0.875")
_PURITY_18K = Decimal("0.750")


def build_rate_history_row(value: Decimal, source: str) -> "GoldRateHistory":
    """Build a GoldRateHistory row with all four karats populated (Phase 6).

    The feed only quotes 24K, so the other karats are derived via purity at poll
    time and stored exactly (per_karat_backfilled=False — these are live points,
    not the one-time historical backfill)."""
    value = Decimal(str(value))
    return GoldRateHistory(
        rate_24k=value,
        rate_22k=(value * _PURITY_22K).quantize(Decimal("0.01")),
        rate_21k=(value * _PURITY_21K).quantize(Decimal("0.01")),
        rate_18k=(value * _PURITY_18K).quantize(Decimal("0.01")),
        per_karat_backfilled=False,
        source=source,
    )


async def _fetch_goldapi() -> Decimal:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            settings.gold_api_url,
            headers={"x-access-token": settings.gold_api_key},
        )
        r.raise_for_status()
        data = r.json()
        return Decimal(str(data["price_gram_24k"]))


async def _fetch_lbma() -> Decimal:
    """Fallback: use LBMA reference (simplified — returns last known value from a public feed)."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://goldprice.org/",
    }
    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        r = await client.get("https://data-asg.goldprice.org/dbXRates/USD")
        r.raise_for_status()
        data = r.json()
        # LBMA feed returns oz price; convert to gram (1 troy oz = 31.1035g)
        price_oz = Decimal(str(data["items"][0]["xauPrice"]))
        return (price_oz / Decimal("31.1035")).quantize(Decimal("0.01"))


async def fetch_gold_rate() -> GoldRateResult:
    try:
        value = await _fetch_goldapi()
        return GoldRateResult(value=value, source="goldapi")
    except Exception as e:
        log.warning("GoldAPI failed: %s — trying LBMA", e)

    try:
        value = await _fetch_lbma()
        return GoldRateResult(value=value, source="lbma")
    except Exception as e:
        log.error("LBMA fallback also failed: %s", e)
        raise


async def get_current_gold_rate(db: AsyncSession) -> dict:
    override = (
        await db.execute(
            select(GoldRateOverride).where(GoldRateOverride.is_active.is_(True)).limit(1)
        )
    ).scalar_one_or_none()

    if override:
        return {
            "rate": float(override.rate_24k),
            "source": "override",
            "fetched_at": override.set_at,
            "is_stale": False,
            "market_closed": False,
        }

    latest = (
        await db.execute(
            select(GoldRateHistory).order_by(GoldRateHistory.fetched_at.desc()).limit(1)
        )
    ).scalar_one_or_none()

    if not latest:
        raise RuntimeError("No gold rate available — run the poller or seed a rate")

    age_seconds = (datetime.now(timezone.utc) - latest.fetched_at.replace(tzinfo=timezone.utc)).total_seconds()
    # Phase 6 (#6): sustained staleness == "market closed / feed down". Threshold
    # mirrors the poller's Discord-alert trigger: failure_threshold × refresh
    # interval (default 3 × 15 min = 45 min) so the banner and the Discord alert
    # appear together. Falls back to 45 min if config is unset.
    refresh_min = settings.gold_refresh_minutes or 15
    stale_threshold = refresh_min * 60
    closed_threshold = max(stale_threshold * 2, (settings.gold_alert_failure_threshold or 3) * refresh_min * 60)
    return {
        "rate": float(latest.rate_24k),
        "source": "live",
        "fetched_at": latest.fetched_at,
        "is_stale": age_seconds > stale_threshold,
        "market_closed": age_seconds > closed_threshold,
    }
