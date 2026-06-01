"""Phase 0 checkpoint 0.5 — date-range buckets are correct across the Beirut
timezone boundary (including DST), and intervals are half-open and contiguous."""
from datetime import date, datetime, timezone

from app.core.daterange import (
    BEIRUT_TZ,
    day_range,
    month_range,
    resolve_range,
    year_range,
)


def test_day_range_winter_is_utc_plus_2():
    # Beirut is UTC+2 in winter (no DST). 2026-01-15 local 00:00 == 2026-01-14 22:00 UTC.
    start, end = day_range(date(2026, 1, 15))
    assert start == datetime(2026, 1, 14, 22, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 1, 15, 22, 0, tzinfo=timezone.utc)


def test_day_range_summer_is_utc_plus_3():
    # Beirut is UTC+3 in summer (DST). 2026-07-15 local 00:00 == 2026-07-14 21:00 UTC.
    start, end = day_range(date(2026, 7, 15))
    assert start == datetime(2026, 7, 14, 21, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 7, 15, 21, 0, tzinfo=timezone.utc)


def test_a_1am_beirut_sale_buckets_into_the_local_day_not_the_utc_day():
    # A sale at 2026-07-15 01:00 Beirut == 2026-07-14 22:00 UTC. A naive UTC-date
    # bucket would file it under the 14th; our range files it under the 15th.
    sale_utc = datetime(2026, 7, 15, 1, 0, tzinfo=BEIRUT_TZ).astimezone(timezone.utc)
    start, end = day_range(date(2026, 7, 15))
    assert start <= sale_utc < end
    prev_start, prev_end = day_range(date(2026, 7, 14))
    assert not (prev_start <= sale_utc < prev_end)


def test_adjacent_days_are_contiguous_and_non_overlapping():
    _, end1 = day_range(date(2026, 3, 10))
    start2, _ = day_range(date(2026, 3, 11))
    assert end1 == start2  # half-open intervals tile the timeline


def test_month_range_spans_full_local_month():
    start, end = month_range(2026, 2, BEIRUT_TZ)
    assert start == datetime(2026, 1, 31, 22, 0, tzinfo=timezone.utc)  # Feb 1 00:00 +02
    assert end == datetime(2026, 2, 28, 22, 0, tzinfo=timezone.utc)    # Mar 1 00:00 +02


def test_month_range_december_rolls_to_next_year():
    start, end = month_range(2026, 12)
    assert start == datetime(2026, 11, 30, 22, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 12, 31, 22, 0, tzinfo=timezone.utc)


def test_year_range_spans_full_local_year():
    start, end = year_range(2026)
    assert start == datetime(2025, 12, 31, 22, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 12, 31, 22, 0, tzinfo=timezone.utc)


def test_resolve_range_dispatch():
    anchor = date(2026, 7, 15)
    assert resolve_range("day", anchor) == day_range(anchor)
    assert resolve_range("month", anchor) == month_range(2026, 7)
    assert resolve_range("year", anchor) == year_range(2026)


def test_resolve_range_rejects_unknown_granularity():
    try:
        resolve_range("week", date(2026, 7, 15))
    except ValueError as e:
        assert "granularity" in str(e)
    else:
        raise AssertionError("expected ValueError for unknown granularity")
