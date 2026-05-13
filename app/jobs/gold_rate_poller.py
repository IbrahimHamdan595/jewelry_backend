import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.gold_api import fetch_gold_rate
from app.db.session import async_session_factory
from app.models import GoldRateHistory

log = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


async def _poll_once() -> None:
    try:
        rate = await fetch_gold_rate()
        async with async_session_factory() as db:
            db.add(GoldRateHistory(rate_24k=rate.value, source=rate.source))
            await db.commit()
        log.info("Gold rate updated: %.2f (%s)", rate.value, rate.source)
    except Exception:
        log.exception("Gold rate fetch failed")


def start_gold_rate_poller(interval_minutes: int = 15) -> None:
    scheduler.add_job(
        _poll_once,
        "interval",
        minutes=interval_minutes,
        id="gold-rate",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc),
    )
    scheduler.start()
    log.info("Gold rate poller started (every %d min, firing immediately)", interval_minutes)
