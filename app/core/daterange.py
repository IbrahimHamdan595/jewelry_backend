"""Calendar date-range helper (Phase 0).

`created_at` / `occurred_at` are stored as UTC-aware timestamps, but the shop
thinks in **Beirut local calendar days**. A naive `WHERE created_at::date = ?`
would bucket a 1 AM Beirut sale into the wrong UTC day. This module converts a
local calendar selection (day / month / year) into the half-open UTC interval
`[start, end)` to filter on, with DST handled by `zoneinfo`.

Used by the orders/buybacks/supplier-purchase list endpoints and the unified
orders page (Phase 5). Always filter `>= start AND < end` (half-open) so the
boundary instant belongs to exactly one bucket.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException

# The shop's wall-clock timezone. Single source of truth for calendar bucketing.
BEIRUT_TZ = ZoneInfo("Asia/Beirut")

Granularity = str  # "day" | "month" | "year"


def _local_midnight(d: date, tz: ZoneInfo) -> datetime:
    """Midnight at the start of local calendar day `d`, as a UTC instant."""
    local = datetime.combine(d, time.min, tzinfo=tz)
    return local.astimezone(timezone.utc)


def day_range(d: date, tz: ZoneInfo = BEIRUT_TZ) -> tuple[datetime, datetime]:
    """UTC [start, end) spanning the single local calendar day `d`."""
    start = _local_midnight(d, tz)
    # Add a day in local terms (handles DST: a local day may be 23/24/25h).
    next_day = date.fromordinal(d.toordinal() + 1)
    end = _local_midnight(next_day, tz)
    return start, end


def month_range(year: int, month: int, tz: ZoneInfo = BEIRUT_TZ) -> tuple[datetime, datetime]:
    """UTC [start, end) spanning the local calendar month."""
    start = _local_midnight(date(year, month, 1), tz)
    if month == 12:
        next_first = date(year + 1, 1, 1)
    else:
        next_first = date(year, month + 1, 1)
    end = _local_midnight(next_first, tz)
    return start, end


def year_range(year: int, tz: ZoneInfo = BEIRUT_TZ) -> tuple[datetime, datetime]:
    """UTC [start, end) spanning the local calendar year."""
    start = _local_midnight(date(year, 1, 1), tz)
    end = _local_midnight(date(year + 1, 1, 1), tz)
    return start, end


def resolve_range(
    granularity: Granularity, anchor: date, tz: ZoneInfo = BEIRUT_TZ
) -> tuple[datetime, datetime]:
    """Dispatch to day/month/year range based on `granularity`.

    `anchor` is any local date inside the desired bucket (its day component is
    ignored for month/year). Raises ValueError on an unknown granularity.
    """
    if granularity == "day":
        return day_range(anchor, tz)
    if granularity == "month":
        return month_range(anchor.year, anchor.month, tz)
    if granularity == "year":
        return year_range(anchor.year, tz)
    raise ValueError(f"unknown granularity {granularity!r}; expected day|month|year")


def parse_calendar_filter(
    granularity: str, date_str: str, tz: ZoneInfo = BEIRUT_TZ
) -> tuple[datetime, datetime] | None:
    """Parse list-endpoint query params (`granularity`, `date`) into a UTC
    [start, end) range, or None when no filter was requested.

    Both must be supplied together. Raises HTTP 422 on a bad granularity or an
    unparseable date so API callers get a clean error instead of a 500.
    """
    if not granularity or not date_str:
        return None
    try:
        anchor = date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid date '{date_str}'; expected YYYY-MM-DD")
    try:
        return resolve_range(granularity, anchor, tz)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
