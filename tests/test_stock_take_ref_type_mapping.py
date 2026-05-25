"""Tests for the StockTakeRefType → AdjustmentTarget mapping.

AUDIT: this mapping is the only thing standing between a stock-take
approval and an adjustment posted against the wrong inventory table.
Tested in isolation here so a regression surfaces independently of any
endpoint or workflow test.
"""
import pytest

from app.core.stock_take import to_adjustment_target
from app.models import AdjustmentTarget, StockTakeRefType


def test_coin_stock_maps_to_adjustment_target_coin_stock():
    assert to_adjustment_target(StockTakeRefType.COIN_STOCK) == AdjustmentTarget.COIN_STOCK


def test_ounce_stock_maps_to_adjustment_target_ounce_stock():
    assert to_adjustment_target(StockTakeRefType.OUNCE_STOCK) == AdjustmentTarget.OUNCE_STOCK


def test_every_ref_type_has_a_mapping():
    """Regression guard: if someone adds a new value to StockTakeRefType
    without updating the mapping, this test fails immediately and points
    at the missing value."""
    for ref_type in StockTakeRefType:
        target = to_adjustment_target(ref_type)
        # Returned target must be a valid AdjustmentTarget — not None,
        # not the wrong type, not silently coerced.
        assert isinstance(target, AdjustmentTarget), (
            f"{ref_type!r} mapped to non-AdjustmentTarget {target!r}"
        )


def test_unknown_value_raises_keyerror():
    """Defensive: a value not in the mapping must raise, not silently
    return a default. (Note: passing a bare str whose value matches an
    existing enum member DOES succeed because StockTakeRefType is a
    `str, Enum` and string == enum-member-with-same-value for dict
    lookup — that's a Python str-enum feature, not a categorization
    bug. The test below uses a value that isn't anywhere in the enum.)"""
    with pytest.raises(KeyError):
        to_adjustment_target("NOT_A_REAL_REF_TYPE")  # type: ignore[arg-type]


def test_mapping_is_injective_for_known_values():
    """No two ref_types map to the same target — if they did, an
    approval against either ref_type would land at the same place,
    which would be a categorization bug."""
    targets = [to_adjustment_target(rt) for rt in StockTakeRefType]
    assert len(targets) == len(set(targets))
