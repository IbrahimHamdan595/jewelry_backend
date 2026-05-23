import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings
from app.core.gold_api import fetch_gold_rate
from app.core.notify import send_discord_alert
from app.db.session import async_session_factory
from app.models import GoldRateHistory

log = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()

_state = {"consecutive_failures": 0, "alerted": False}


async def _poll_once() -> None:
    try:
        rate = await fetch_gold_rate()
        async with async_session_factory() as db:
            db.add(GoldRateHistory(rate_24k=rate.value, source=rate.source))
            await db.commit()
        log.info("Gold rate updated: %.2f (%s)", rate.value, rate.source)

        if _state["alerted"]:
            await send_discord_alert(
                f":white_check_mark: Gold rate feed recovered after "
                f"{_state['consecutive_failures']} failed attempts. "
                f"Latest: {rate.value} ({rate.source})."
            )
        _state["consecutive_failures"] = 0
        _state["alerted"] = False
    except Exception as e:
        _state["consecutive_failures"] += 1
        log.exception("Gold rate fetch failed (attempt %d)", _state["consecutive_failures"])

        if (
            not _state["alerted"]
            and _state["consecutive_failures"] >= settings.gold_alert_failure_threshold
        ):
            await send_discord_alert(
                f":warning: Gold rate feed is stale — {_state['consecutive_failures']} "
                f"consecutive failures. Last error: `{type(e).__name__}: {e}`. "
                f"Customers are being served the last cached rate.",
                mention=True,
            )
            _state["alerted"] = True


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
