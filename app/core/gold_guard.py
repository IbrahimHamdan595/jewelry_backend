"""Stale-rate guard for money-moving endpoints.

The POS is designed to keep trading when the gold feed is unreachable (see
`docs/superpowers/specs/2026-08-01-stale-gold-rate-sale-guard-design.md`). It
serves the last known rate and flags it `market_closed` once it is badly stale.
Before this module, `create_order` and `create_buyback` read that rate and
ignored the flag entirely — a cashier could complete a sale, or buy gold off the
street, on a price nobody had confirmed was still real.

The rule: once the rate is `market_closed` and no admin override is active, the
request must carry an explicit acknowledgement naming the exact rate timestamp
the cashier accepted. Blocking outright was considered and rejected — it freezes
a busy counter until the admin is reachable. A confirmation keeps the shop
trading and leaves a signed, hash-chained record of who chose to trade on an old
price.

The ack carries a timestamp rather than a boolean on purpose: a boolean can be
hardcoded `true` by a client forever, and the resulting audit row would prove
nothing.
"""
from datetime import datetime, timezone

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ledger import EVENT_SALE_ON_STALE_RATE_ACK, record


class StaleRateAck(BaseModel):
    """A cashier's explicit acceptance of one specific stale rate."""

    rate_fetched_at: datetime


def _as_utc(value: datetime) -> datetime:
    """Normalise to aware-UTC.

    `get_current_gold_rate` returns `fetched_at` straight off the model, which is
    naive UTC; the ack arrives from JSON as aware. Comparing the two directly
    raises TypeError, so both sides go through here first.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _age_minutes(fetched_at: datetime) -> int:
    return int((datetime.now(timezone.utc) - fetched_at).total_seconds() // 60)


def assert_rate_acceptable(rate_info: dict, ack: StaleRateAck | None) -> None:
    """Raise 409 unless it is safe — or explicitly accepted — to trade on this rate.

    Returns None and lets the caller proceed unchanged. This is a gate, not a
    transform.
    """
    # (1) An active override is a deliberate, admin-set, already-audited price.
    # Redundant with (2) today, because the override branch of
    # get_current_gold_rate also sets market_closed=False — stated explicitly so
    # the shop's offline escape hatch does not depend on that staying true.
    if rate_info.get("source") == "override":
        return

    # (2) The normal case: rate is fresh enough to trade on unchallenged.
    if not rate_info.get("market_closed"):
        return

    fetched_at = _as_utc(rate_info["fetched_at"])
    age = _age_minutes(fetched_at)

    # (3) Stale and unacknowledged. The body must carry everything a client needs
    # to render the confirmation, because a stale browser tab (or any non-POS API
    # consumer) hits this without ever having seen the banner.
    if ack is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "STALE_RATE_ACK_REQUIRED",
                "message": (
                    f"Gold rate has not refreshed since {fetched_at.isoformat()} "
                    f"({age} minutes ago)."
                ),
                "rate_24k": rate_info["rate"],
                "rate_fetched_at": fetched_at.isoformat(),
                "age_minutes": age,
            },
        )

    # (4) Acknowledged, but not the rate we are about to charge. The feed
    # recovered between the dialog and the submit, or the tab is stale. Either
    # way the cashier confirmed a different number.
    if _as_utc(ack.rate_fetched_at) != fetched_at:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "STALE_RATE_ACK_MISMATCH",
                "message": (
                    "The gold rate changed since you confirmed it. "
                    "Please review and confirm again."
                ),
                "rate_24k": rate_info["rate"],
                "rate_fetched_at": fetched_at.isoformat(),
                "age_minutes": age,
            },
        )


async def record_stale_rate_ack(
    db: AsyncSession,
    *,
    actor_user_id: str,
    rate_info: dict,
    ref_type: str,
    ref_id: str,
    context: str,
    ack: StaleRateAck | None,
) -> None:
    """Append the SALE_ON_STALE_RATE_ACK ledger row. No-op when `ack` is None.

    Does NOT commit — the row must land in the caller's transaction so it is
    atomic with the order/buyback it justifies. A committed sale with no
    justification row is exactly the state the hash chain exists to make
    impossible.
    """
    if ack is None:
        return

    fetched_at = _as_utc(rate_info["fetched_at"])
    await record(
        db,
        event_type=EVENT_SALE_ON_STALE_RATE_ACK,
        actor_user_id=actor_user_id,
        ref_type=ref_type,
        ref_id=ref_id,
        payload={
            "rate_24k": str(rate_info["rate"]),
            "rate_fetched_at": fetched_at.isoformat(),
            "age_minutes": _age_minutes(fetched_at),
            "context": context,
        },
    )
