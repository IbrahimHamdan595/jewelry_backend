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
        }

    latest = (
        await db.execute(
            select(GoldRateHistory).order_by(GoldRateHistory.fetched_at.desc()).limit(1)
        )
    ).scalar_one_or_none()

    if not latest:
        raise RuntimeError("No gold rate available — run the poller or seed a rate")

    age_seconds = (datetime.now(timezone.utc) - latest.fetched_at.replace(tzinfo=timezone.utc)).total_seconds()
    return {
        "rate": float(latest.rate_24k),
        "source": "live",
        "fetched_at": latest.fetched_at,
        "is_stale": age_seconds > 15 * 60,
    }
