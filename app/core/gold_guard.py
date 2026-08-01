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

The ack carries a timestamp rather than a boolean on purpose. A boolean can be
hardcoded `true` by a client forever; a timestamp at least stops being valid the
moment the feed recovers, and it pins each audit row to the exact price that was
accepted. It does not prove a human looked — nothing at this layer can.

NOTE: `detail` here is a dict, not the plain string used by the other ~100
HTTPException sites in this codebase — a client needs `code` and
`rate_fetched_at` to render the confirmation. `frontend/src/lib/api-client.ts`
is updated (later task) to preserve structured detail; it previously stringified
it, which would render as "[object Object]".
"""
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ledger import EVENT_SALE_ON_STALE_RATE_ACK, record

# Tolerance for comparing an ack's timestamp to the rate's `fetched_at`. See
# the mismatch check in assert_rate_acceptable for why this isn't exact
# equality.
_ACK_TOLERANCE = timedelta(seconds=1)


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
    """Whole minutes since the rate was fetched, floored at 0.

    Clamped because app-vs-DB clock skew can put a DB-side now() timestamp in the
    future, and "(-90 minutes ago)" is not something to show a cashier.
    """
    seconds = (datetime.now(timezone.utc) - fetched_at).total_seconds()
    return max(0, int(seconds // 60))


def _requires_ack(rate_info: dict) -> bool:
    """True when this rate may only be traded on with an explicit acknowledgement.

    An active override is a deliberate, admin-set, already-audited price — stated
    explicitly rather than relying on get_current_gold_rate continuing to set
    market_closed=False for overrides.
    """
    if rate_info.get("source") == "override":
        return False
    return bool(rate_info.get("market_closed"))


def assert_rate_acceptable(rate_info: dict, ack: StaleRateAck | None) -> None:
    """Raise 409 unless it is safe — or explicitly accepted — to trade on this rate."""
    # `rate` and `fetched_at` are contractually always present (non-nullable
    # columns); the flags are treated as optional here, matching
    # gold_price.py:35's `info.get("market_closed", False)`.
    if not _requires_ack(rate_info):
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
    #
    # Tolerance, not identity. Postgres stores microseconds (server_default=
    # now()); JS Date — and most client date libraries — are millisecond-
    # precision, so a round-tripped timestamp loses digits and would never
    # match. Rates are at least 10 minutes apart, so a 1s window still pins
    # the ack to exactly one rate, while a strict compare would hard-block the
    # till for the whole outage with a 409 no cashier could clear.
    if abs(_as_utc(ack.rate_fetched_at) - fetched_at) >= _ACK_TOLERANCE:
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
    context: Literal["ORDER", "BUYBACK"],
    ack: StaleRateAck | None,
) -> None:
    """Append the SALE_ON_STALE_RATE_ACK ledger row.

    No-op unless an ack was actually required and supplied — a client that
    attaches an ack unconditionally (e.g. to never show the dialog twice) must
    not pollute the ledger with a row on every ordinary fresh-rate or
    admin-override sale. That would make the event useless to an auditor
    trying to find the sales that actually traded on a stale price.

    Does NOT commit — the row must land in the caller's transaction so it is
    atomic with the order/buyback it justifies. A committed sale with no
    justification row is exactly the state the hash chain exists to make
    impossible.
    """
    if ack is None or not _requires_ack(rate_info):
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
