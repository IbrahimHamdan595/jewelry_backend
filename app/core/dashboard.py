"""Dashboard computation helpers (Phase A-E).

Each function takes an AsyncSession + a UTC [start, end) window (or an as-of
date) and returns JSON-friendly primitives. Kept out of app/api/reports.py so
each unit is independently testable. Windows are Beirut-local calendar days
(see app/core/daterange).
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.core.daterange import BEIRUT_TZ, day_range

ZERO = Decimal("0")
_Q_MONEY = Decimal("0.01")
_Q_GRAMS = Decimal("0.001")


def week_window(today: date) -> tuple[datetime, datetime]:
    """UTC [start, end) spanning the 7 Beirut calendar days ending on `today`."""
    start = day_range(today - timedelta(days=6))[0]
    end = day_range(today)[1]
    return start, end
