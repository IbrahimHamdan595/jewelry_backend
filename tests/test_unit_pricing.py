"""Tests for the coin/ounce pricing helper.

Formula (per spec §6 Phase 2):
    effective_rate = rate_24k + markup_per_gram   # markup is signed
    metal_value    = effective_rate * weight_grams
    final_price    = metal_value + margin

where `margin` is either a flat USD amount (`margin_mode=USD`) or a
percentage of `metal_value` (`margin_mode=PERCENT`).

Karat is metadata on coin/ounce types and does NOT enter this formula —
coins trade at numismatic premiums/discounts that markup absorbs.
"""

from decimal import Decimal

import pytest

from app.core.pricing import calculate_unit_price


def test_flat_usd_margin_with_positive_markup():
    # rate=70/g, markup=+5/g, 10g coin, flat $20 margin
    # effective=75, metal=750, final=750+20=770
    result = calculate_unit_price(
        rate_24k=Decimal("70"),
        weight_grams=Decimal("10"),
        markup_per_gram=Decimal("5"),
        margin_mode="USD",
        margin_value=Decimal("20"),
    )
    assert result["effective_rate"] == Decimal("75.00")
    assert result["metal_value"] == Decimal("750.00")
    assert result["margin_amount"] == Decimal("20.00")
    assert result["final_price"] == Decimal("770.00")


def test_flat_usd_margin_with_negative_markup():
    # rate=70/g, markup=-3/g, 8g coin (some coins trade below spot)
    # effective=67, metal=536, final=536+10=546
    result = calculate_unit_price(
        rate_24k=Decimal("70"),
        weight_grams=Decimal("8"),
        markup_per_gram=Decimal("-3"),
        margin_mode="USD",
        margin_value=Decimal("10"),
    )
    assert result["effective_rate"] == Decimal("67.00")
    assert result["metal_value"] == Decimal("536.00")
    assert result["margin_amount"] == Decimal("10.00")
    assert result["final_price"] == Decimal("546.00")


def test_percent_margin():
    # rate=100/g, markup=0, 5g, 12% margin
    # effective=100, metal=500, margin=60, final=560
    result = calculate_unit_price(
        rate_24k=Decimal("100"),
        weight_grams=Decimal("5"),
        markup_per_gram=Decimal("0"),
        margin_mode="PERCENT",
        margin_value=Decimal("12"),
    )
    assert result["effective_rate"] == Decimal("100.00")
    assert result["metal_value"] == Decimal("500.00")
    assert result["margin_amount"] == Decimal("60.00")
    assert result["final_price"] == Decimal("560.00")


def test_quantization_to_two_decimals():
    # Force a result with more than 2 decimal places of precision
    # rate=71.234, weight=3.333, markup=0, margin USD 0
    # metal = 71.234 * 3.333 = 237.422322 → rounds to 237.42
    result = calculate_unit_price(
        rate_24k=Decimal("71.234"),
        weight_grams=Decimal("3.333"),
        markup_per_gram=Decimal("0"),
        margin_mode="USD",
        margin_value=Decimal("0"),
    )
    assert result["metal_value"] == Decimal("237.42")
    assert result["final_price"] == Decimal("237.42")


def test_invalid_margin_mode_rejected():
    with pytest.raises(ValueError):
        calculate_unit_price(
            rate_24k=Decimal("70"),
            weight_grams=Decimal("10"),
            markup_per_gram=Decimal("0"),
            margin_mode="BOGUS",
            margin_value=Decimal("0"),
        )


def test_returned_keys():
    result = calculate_unit_price(
        rate_24k=Decimal("70"),
        weight_grams=Decimal("10"),
        markup_per_gram=Decimal("0"),
        margin_mode="USD",
        margin_value=Decimal("0"),
    )
    assert set(result.keys()) == {
        "effective_rate",
        "metal_value",
        "margin_amount",
        "final_price",
    }
