"""Tests for the walk-in buyback price helper.

Formula (per spec §6 Phase 3):
    purity_rate    = rate_24k * KARAT_PURITY[karat]
    if USD_PER_GRAM: effective_per_g = purity_rate - margin_value
                     buy_price       = effective_per_g * weight_grams
    if PERCENT:      effective_per_g = purity_rate * (1 - margin_value/100)
                     buy_price       = effective_per_g * weight_grams

The margin is the shop's spread when *buying* gold from a walk-in seller.
"""

from decimal import Decimal

import pytest

from app.core.pricing import compute_buyback_price
from app.models import Karat


def test_usd_per_gram_margin_k21():
    # rate=80/g, K21 purity=0.875 → purity_rate=70/g
    # margin=2/g → effective=68/g; weight=10g → buy=680.00
    result = compute_buyback_price(
        rate_24k=Decimal("80"),
        karat=Karat.K21,
        weight_grams=Decimal("10"),
        margin_mode="USD_PER_GRAM",
        margin_value=Decimal("2"),
    )
    assert result["purity_rate"] == Decimal("70.00")
    assert result["effective_rate_per_gram"] == Decimal("68.00")
    assert result["buy_price"] == Decimal("680.00")


def test_percent_margin_k24():
    # rate=100/g, K24 purity=0.999 → purity_rate=99.9/g
    # 5% margin → effective=99.9 * 0.95 = 94.905/g
    # weight=5 → buy=474.525 → rounds to 474.53
    result = compute_buyback_price(
        rate_24k=Decimal("100"),
        karat=Karat.K24,
        weight_grams=Decimal("5"),
        margin_mode="PERCENT",
        margin_value=Decimal("5"),
    )
    assert result["purity_rate"] == Decimal("99.90")
    assert result["effective_rate_per_gram"] == Decimal("94.91")
    assert result["buy_price"] == Decimal("474.53")


def test_k22_supported():
    # K22 purity=0.917 → rate*purity at rate=100 → 91.70
    # zero margin → buy = 91.70 * 8 = 733.60
    result = compute_buyback_price(
        rate_24k=Decimal("100"),
        karat=Karat.K22,
        weight_grams=Decimal("8"),
        margin_mode="USD_PER_GRAM",
        margin_value=Decimal("0"),
    )
    assert result["purity_rate"] == Decimal("91.70")
    assert result["buy_price"] == Decimal("733.60")


def test_zero_margin_pays_full_metal_value():
    result = compute_buyback_price(
        rate_24k=Decimal("70"),
        karat=Karat.K18,  # 0.750
        weight_grams=Decimal("4"),
        margin_mode="USD_PER_GRAM",
        margin_value=Decimal("0"),
    )
    # purity_rate = 52.50; effective=52.50; buy = 210.00
    assert result["buy_price"] == Decimal("210.00")


def test_margin_cannot_push_buy_price_negative():
    # If margin > purity_rate the formula would yield a negative buy price;
    # the helper must reject this to prevent paying-from-customer scenarios.
    with pytest.raises(ValueError):
        compute_buyback_price(
            rate_24k=Decimal("70"),
            karat=Karat.K18,  # purity_rate = 52.50
            weight_grams=Decimal("4"),
            margin_mode="USD_PER_GRAM",
            margin_value=Decimal("100"),
        )


def test_invalid_margin_mode():
    with pytest.raises(ValueError):
        compute_buyback_price(
            rate_24k=Decimal("70"),
            karat=Karat.K18,
            weight_grams=Decimal("4"),
            margin_mode="BOGUS",
            margin_value=Decimal("1"),
        )


def test_percent_margin_above_100_rejected():
    with pytest.raises(ValueError):
        compute_buyback_price(
            rate_24k=Decimal("100"),
            karat=Karat.K24,
            weight_grams=Decimal("1"),
            margin_mode="PERCENT",
            margin_value=Decimal("105"),
        )


def test_returned_keys():
    result = compute_buyback_price(
        rate_24k=Decimal("70"),
        karat=Karat.K18,
        weight_grams=Decimal("1"),
        margin_mode="USD_PER_GRAM",
        margin_value=Decimal("0"),
    )
    assert set(result.keys()) == {
        "purity_rate",
        "effective_rate_per_gram",
        "buy_price",
    }
