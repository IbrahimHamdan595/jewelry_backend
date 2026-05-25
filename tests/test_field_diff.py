"""Tests for field_diff — the helper that powers SETTINGS_CHANGED /
STAFF_UPDATED audit payloads.

AUDIT RATIONALE
---------------
field_diff is what an auditor reads when asking "what changed?" so it must:
  • return only the keys that actually changed (no noise)
  • emit consistent {from, to} structure for every change
  • normalize Decimal / datetime values so the JSON payload doesn't lose
    precision or carry untyped Python repr
  • handle key add/remove (a new setting appearing, an old one removed)
"""
from datetime import date, datetime, timezone
from decimal import Decimal

from app.core.ledger import field_diff


def test_returns_only_changed_fields():
    before = {"a": 1, "b": 2, "c": 3}
    after = {"a": 1, "b": 99, "c": 3}
    assert field_diff(before, after) == {"b": {"from": 2, "to": 99}}


def test_empty_diff_when_nothing_changed():
    d = {"a": 1, "b": "x"}
    assert field_diff(d, d.copy()) == {}


def test_handles_key_added():
    before = {"a": 1}
    after = {"a": 1, "b": 2}
    assert field_diff(before, after) == {"b": {"from": None, "to": 2}}


def test_handles_key_removed():
    before = {"a": 1, "b": 2}
    after = {"a": 1}
    assert field_diff(before, after) == {"b": {"from": 2, "to": None}}


def test_decimal_normalized_to_string():
    """Decimals must survive JSON serialization with full precision —
    str() preserves trailing zeros set by quantize()."""
    before = {"vat_percent": Decimal("11.00")}
    after = {"vat_percent": Decimal("12.50")}
    assert field_diff(before, after) == {
        "vat_percent": {"from": "11.00", "to": "12.50"}
    }


def test_decimal_unchanged_skipped_even_if_object_identity_differs():
    """Two Decimal instances with the same value compare equal, so the
    diff should treat them as unchanged."""
    before = {"x": Decimal("100.000")}
    after = {"x": Decimal("100.000")}
    assert field_diff(before, after) == {}


def test_datetime_normalized_to_isoformat():
    t1 = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    diff = field_diff({"updated_at": t1}, {"updated_at": t2})
    assert diff == {
        "updated_at": {
            "from": "2026-05-25T12:00:00+00:00",
            "to": "2026-05-26T12:00:00+00:00",
        }
    }


def test_date_normalized_to_isoformat():
    d1 = date(2026, 5, 25)
    d2 = date(2026, 5, 26)
    diff = field_diff({"d": d1}, {"d": d2})
    assert diff == {"d": {"from": "2026-05-25", "to": "2026-05-26"}}


def test_none_to_value_recorded_as_from_null():
    """First-time set of an optional field."""
    before = {"reason": None}
    after = {"reason": "manual correction"}
    assert field_diff(before, after) == {
        "reason": {"from": None, "to": "manual correction"}
    }


def test_bool_change_explicit():
    """is_active flip — STAFF_UPDATED relies on this for soft-delete."""
    diff = field_diff({"is_active": True}, {"is_active": False})
    assert diff == {"is_active": {"from": True, "to": False}}


def test_multiple_changes_aggregated():
    before = {
        "vat_percent": Decimal("11.00"),
        "nisab_grams": Decimal("85.000"),
        "store_name": "Old",
    }
    after = {
        "vat_percent": Decimal("12.00"),
        "nisab_grams": Decimal("85.000"),  # unchanged
        "store_name": "New",
    }
    diff = field_diff(before, after)
    assert set(diff.keys()) == {"vat_percent", "store_name"}
    assert diff["vat_percent"] == {"from": "11.00", "to": "12.00"}
    assert diff["store_name"] == {"from": "Old", "to": "New"}


def test_unknown_type_falls_back_to_str():
    """Anything not a primitive or container gets str()'d so it can still
    be JSON-encoded. This keeps the helper defensive against future model
    fields carrying custom types."""

    class Custom:
        def __str__(self):
            return "custom-repr"

    diff = field_diff({"x": Custom()}, {"x": None})
    assert diff == {"x": {"from": "custom-repr", "to": None}}
