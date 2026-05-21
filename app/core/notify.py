import logging

import httpx

from app.config import settings

log = logging.getLogger(__name__)


async def send_discord_alert(message: str, mention: bool = False) -> None:
    if not settings.discord_webhook_url:
        log.debug("Discord webhook not configured; skipping alert: %s", message)
        return

    content = message
    payload: dict = {}
    if mention and settings.discord_alert_user_id:
        content = f"<@{settings.discord_alert_user_id}> {message}"
        payload["allowed_mentions"] = {"users": [settings.discord_alert_user_id]}
    payload["content"] = content[:1900]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(settings.discord_webhook_url, json=payload)
            r.raise_for_status()
    except Exception:
        log.exception("Failed to send Discord alert")
