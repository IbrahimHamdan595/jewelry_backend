from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.core import dashboard
from app.core.daterange import BEIRUT_TZ, day_range


def test_dashboard_module_importable():
    assert hasattr(dashboard, "week_window")


def test_week_window_is_seven_beirut_days():
    start, end = dashboard.week_window(date(2026, 6, 5))
    assert end == day_range(date(2026, 6, 5))[1]
    assert start == day_range(date(2026, 5, 30))[0]
