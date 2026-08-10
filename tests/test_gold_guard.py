"""Stale-rate guard: the rule that gates sales/buybacks on a market_closed rate.

Pure unit tests — no DB. `rate_info` dicts are hand-built to the shape
`app.core.gold_api.get_current_gold_rate` returns, including cases that
function does not currently produce (see test_override_passes_even_if_flagged).
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from fastapi import HTTPException

from app.core.gold_guard import StaleRateAck, assert_rate_acceptable, record_stale_rate_ack
from app.models import InventoryLedger

# Relative to "now" rather than pinned to today's date, so this suite doesn't
# quietly rot as the calendar moves on.
_FETCHED_AT = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=3)
FETCHED_AWARE = _FETCHED_AT
FETCHED_NAIVE = _FETCHED_AT.replace(tzinfo=None)  # naive UTC, as stored in the DB


def _rate_info(*, market_closed: bool, source: str = "live", fetched_at=FETCHED_NAIVE):
    return {
        "rate": 84.31,
        "source": source,
        "fetched_at": fetched_at,
        "is_stale": market_closed,
        "market_closed": market_closed,
    }


def test_fresh_rate_passes_without_ack():
    fresh = _rate_info(market_closed=False, fetched_at=datetime.now(timezone.utc))
    assert_rate_acceptable(fresh, None)  # must not raise


def test_override_passes_even_if_flagged_closed():
    """Rule (1) of the spec, pinned deliberately.

    get_current_gold_rate never produces source="override" together with
    market_closed=True today — the override branch hardcodes both flags False.
    This test exists so that if that branch ever changes, the shop's ONLY way to
    keep trading through an outage does not silently start blocking sales.
    """
    override = _rate_info(market_closed=True, source="override")
    assert_rate_acceptable(override, None)  # must not raise


def test_market_closed_without_ack_is_rejected():
    with pytest.raises(HTTPException) as exc:
        assert_rate_acceptable(_rate_info(market_closed=True), None)
    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert detail["code"] == "STALE_RATE_ACK_REQUIRED"
    assert detail["rate_24k"] == 84.31
    assert detail["rate_fetched_at"] == FETCHED_AWARE.isoformat()
    assert 179 <= detail["age_minutes"] <= 181


def test_matching_ack_passes():
    ack = StaleRateAck(rate_fetched_at=FETCHED_AWARE)
    assert_rate_acceptable(_rate_info(market_closed=True), ack)  # must not raise


def test_mismatched_ack_is_rejected():
    """Feed recovered (or stale tab): the cashier confirmed a different rate."""
    ack = StaleRateAck(rate_fetched_at=FETCHED_AWARE + timedelta(minutes=5))
    with pytest.raises(HTTPException) as exc:
        assert_rate_acceptable(_rate_info(market_closed=True), ack)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "STALE_RATE_ACK_MISMATCH"


def test_naive_and_aware_timestamps_compare_equal():
    """The DB gives naive UTC; JSON gives aware UTC. These are the same instant."""
    ack = StaleRateAck(rate_fetched_at=FETCHED_AWARE)
    info = _rate_info(market_closed=True, fetched_at=FETCHED_NAIVE)
    assert_rate_acceptable(info, ack)  # must not raise TypeError or 409


def test_ack_in_a_different_offset_compares_equal():
    """09:12Z and 12:12+03:00 are the same instant — accept it."""
    other_zone = FETCHED_AWARE.astimezone(timezone(timedelta(hours=3)))
    ack = StaleRateAck(rate_fetched_at=other_zone)
    assert_rate_acceptable(_rate_info(market_closed=True), ack)  # must not raise


def test_ack_truncated_to_milliseconds_still_matches():
    """Postgres stores microseconds; JS Date truncates to milliseconds. The ack
    must survive that round-trip or the till hard-blocks for the whole outage."""
    precise = _FETCHED_AT.replace(microsecond=123456)
    truncated = precise.replace(microsecond=123000)
    info = _rate_info(market_closed=True, fetched_at=precise)
    assert_rate_acceptable(info, StaleRateAck(rate_fetched_at=truncated))  # must not raise


def test_ack_for_a_different_rate_is_still_rejected():
    """The tolerance must not be so loose it stops distinguishing rates."""
    info = _rate_info(market_closed=True, fetched_at=FETCHED_AWARE)
    ack = StaleRateAck(rate_fetched_at=FETCHED_AWARE + timedelta(seconds=90))
    with pytest.raises(HTTPException) as exc:
        assert_rate_acceptable(info, ack)
    assert exc.value.detail["code"] == "STALE_RATE_ACK_MISMATCH"


@pytest.mark.asyncio
async def test_record_writes_one_chained_row_without_committing(db):
    info = _rate_info(market_closed=True)
    await record_stale_rate_ack(
        db,
        actor_user_id="u-1",
        rate_info=info,
        ref_type="order",
        ref_id="o-1",
        context="ORDER",
        ack=StaleRateAck(rate_fetched_at=FETCHED_AWARE),
    )

    rows = (await db.execute(select(InventoryLedger))).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_type == "SALE_ON_STALE_RATE_ACK"
    assert rows[0].ref_type == "order"
    assert rows[0].ref_id == "o-1"
    assert rows[0].payload["context"] == "ORDER"
    assert rows[0].payload["rate_24k"] == "84.31"
    assert rows[0].prev_hash and rows[0].entry_hash != rows[0].prev_hash

    # It must be the CALLER's transaction — a rollback discards it.
    await db.rollback()
    assert (await db.execute(select(InventoryLedger))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "info,ack",
    [
        (_rate_info(market_closed=True), None),
        (_rate_info(market_closed=False), StaleRateAck(rate_fetched_at=FETCHED_AWARE)),
        (_rate_info(market_closed=True, source="override"), StaleRateAck(rate_fetched_at=FETCHED_AWARE)),
    ],
)
async def test_record_is_a_noop_when_no_ack_was_required(db, info, ack):
    """Normal trading must add zero audit rows, even from a client that always
    sends an ack."""
    await record_stale_rate_ack(
        db, actor_user_id="u-1", rate_info=info, ref_type="order",
        ref_id="o-1", context="ORDER", ack=ack,
    )
    assert (await db.execute(select(InventoryLedger))).scalars().all() == []
