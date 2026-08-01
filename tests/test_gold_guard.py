"""Stale-rate guard: the rule that gates sales/buybacks on a market_closed rate.

Pure unit tests — no DB. `rate_info` dicts are hand-built to the shape
`app.core.gold_api.get_current_gold_rate` returns, including cases that
function does not currently produce (see test_override_passes_even_if_flagged).
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.core.gold_guard import StaleRateAck, assert_rate_acceptable

FETCHED_NAIVE = datetime(2026, 8, 1, 9, 12, 0)  # naive UTC, as stored in the DB
FETCHED_AWARE = FETCHED_NAIVE.replace(tzinfo=timezone.utc)


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
    assert detail["age_minutes"] > 0


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
