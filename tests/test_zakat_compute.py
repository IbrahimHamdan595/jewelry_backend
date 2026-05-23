"""Tests for the zakat compute engine — pure-function layer.

Filters / DB integration are covered separately in test_zakat_filters.py
(Phase 3) with a seeded test database. These tests target the pure
`_compute_holdings_from_rows` aggregator and verify:
  • per-karat grouping
  • per-source attribution (products / coins / ounces / lots)
  • coin/ounce quantity multiplication
  • KARAT_PURITY application (re-uses the canonical map from pricing.py)
  • rounding strategy (3dp grams, full precision intermediate)
"""

from decimal import Decimal

import pytest

from app.core.zakat import (
    SOURCE_COINS,
    SOURCE_LOTS,
    SOURCE_OUNCES,
    SOURCE_PRODUCTS,
    _compute_holdings_from_rows,
)
from app.core.pricing import KARAT_PURITY
from app.models import Karat


# ── Happy path: all four sources, all four karats ─────────────────────────────

def test_sums_all_four_types_and_all_four_karats():
    """One row per (source, karat) — proves grouping and source attribution
    survive a fully populated input."""
    products = [
        {"karat": Karat.K18, "weight_grams": Decimal("5.000")},
        {"karat": Karat.K21, "weight_grams": Decimal("10.000")},
    ]
    coins = [
        # 5g per coin × 4 on hand = 20g K22
        {"karat": Karat.K22, "weight_grams": Decimal("5.000"), "on_hand_qty": 4},
    ]
    ounces = [
        # 31.104g per bar × 1 = 31.104g K24
        {"karat": Karat.K24, "weight_grams": Decimal("31.104"), "on_hand_qty": 1},
    ]
    lots = [
        {"karat": Karat.K18, "weight_remaining_grams": Decimal("50.000")},
    ]

    holdings = _compute_holdings_from_rows(
        products=products, coins=coins, ounces=ounces, lots=lots
    )

    by_karat = {b.karat: b for b in holdings.by_karat}

    # K18: 5g product + 50g lot = 55g; Au = 55 * 0.750 = 41.250
    assert by_karat[Karat.K18].grams_by_source[SOURCE_PRODUCTS] == Decimal("5.000")
    assert by_karat[Karat.K18].grams_by_source[SOURCE_LOTS] == Decimal("50.000")
    assert by_karat[Karat.K18].total_weight_grams == Decimal("55.000")
    assert by_karat[Karat.K18].au_grams == Decimal("41.250")

    # K21: 10g product; Au = 10 * 0.875 = 8.750
    assert by_karat[Karat.K21].grams_by_source[SOURCE_PRODUCTS] == Decimal("10.000")
    assert by_karat[Karat.K21].au_grams == Decimal("8.750")

    # K22: 5 * 4 = 20g coins; Au = 20 * 0.917 = 18.340
    assert by_karat[Karat.K22].grams_by_source[SOURCE_COINS] == Decimal("20.000")
    assert by_karat[Karat.K22].au_grams == Decimal("18.340")

    # K24: 31.104g ounce; Au = 31.104 * 0.999 = 31.072896 → 31.073
    assert by_karat[Karat.K24].grams_by_source[SOURCE_OUNCES] == Decimal("31.104")
    assert by_karat[Karat.K24].au_grams == Decimal("31.073")

    # Grand total: 41.250 + 8.750 + 18.340 + 31.073 = 99.413
    # (matches summing au_grams; rounded grand total)
    expected_total = (
        Decimal("55.000") * KARAT_PURITY[Karat.K18]
        + Decimal("10.000") * KARAT_PURITY[Karat.K21]
        + Decimal("20.000") * KARAT_PURITY[Karat.K22]
        + Decimal("31.104") * KARAT_PURITY[Karat.K24]
    ).quantize(Decimal("0.001"))
    assert holdings.total_au_grams == expected_total


# ── Empty inventory ───────────────────────────────────────────────────────────

def test_empty_inventory_returns_zero_au_across_all_karats():
    holdings = _compute_holdings_from_rows(products=[], coins=[], ounces=[], lots=[])

    assert holdings.total_au_grams == Decimal("0.000")
    # Every karat bucket is present (stable shape) and zero on every source.
    karats_seen = {b.karat for b in holdings.by_karat}
    assert karats_seen == {Karat.K18, Karat.K21, Karat.K22, Karat.K24}
    for bucket in holdings.by_karat:
        assert bucket.total_weight_grams == Decimal("0.000")
        assert bucket.au_grams == Decimal("0.000")
        for source_key in (SOURCE_PRODUCTS, SOURCE_COINS, SOURCE_OUNCES, SOURCE_LOTS):
            assert bucket.grams_by_source[source_key] == Decimal("0.000")


# ── Per-source attribution survives multiple rows in the same karat ───────────

def test_multiple_rows_same_karat_same_source_accumulate():
    products = [
        {"karat": Karat.K21, "weight_grams": Decimal("3.500")},
        {"karat": Karat.K21, "weight_grams": Decimal("6.500")},
    ]
    lots = [
        {"karat": Karat.K21, "weight_remaining_grams": Decimal("12.000")},
        {"karat": Karat.K21, "weight_remaining_grams": Decimal("8.000")},
    ]
    holdings = _compute_holdings_from_rows(
        products=products, coins=[], ounces=[], lots=lots
    )
    k21 = next(b for b in holdings.by_karat if b.karat == Karat.K21)
    assert k21.grams_by_source[SOURCE_PRODUCTS] == Decimal("10.000")
    assert k21.grams_by_source[SOURCE_LOTS] == Decimal("20.000")
    assert k21.total_weight_grams == Decimal("30.000")
    # 30 * 0.875 = 26.250
    assert k21.au_grams == Decimal("26.250")


# ── Quantity multiplication for coins/ounces ──────────────────────────────────

def test_coin_quantity_multiplies_weight():
    # 1 sovereign at 7.9881g × 25 on hand = 199.7025g
    coins = [
        {"karat": Karat.K22, "weight_grams": Decimal("7.9881"), "on_hand_qty": 25},
    ]
    holdings = _compute_holdings_from_rows(products=[], coins=coins, ounces=[], lots=[])
    k22 = next(b for b in holdings.by_karat if b.karat == Karat.K22)
    # 7.9881 * 25 = 199.7025 → rounds to 199.703 (3dp HALF_UP)
    assert k22.grams_by_source[SOURCE_COINS] == Decimal("199.703")
    # Au at full precision before final rounding: 199.7025 * 0.917 = 183.126...
    # The bucket rounds to 3dp at the boundary.
    assert k22.au_grams == Decimal("183.127")


def test_ounce_quantity_multiplies_weight():
    # 1oz bar = 31.1035g (troy oz). 3 bars on hand.
    ounces = [
        {"karat": Karat.K24, "weight_grams": Decimal("31.1035"), "on_hand_qty": 3},
    ]
    holdings = _compute_holdings_from_rows(products=[], coins=[], ounces=ounces, lots=[])
    k24 = next(b for b in holdings.by_karat if b.karat == Karat.K24)
    # 31.1035 * 3 = 93.3105 → 93.311
    assert k24.grams_by_source[SOURCE_OUNCES] == Decimal("93.311")
    # 93.3105 * 0.999 = 93.2173... → 93.217
    assert k24.au_grams == Decimal("93.217")


# ── Lots use weight_remaining_grams, not weight_grams ─────────────────────────

def test_lot_uses_weight_remaining_grams_key():
    """The aggregator only ever reads `weight_remaining_grams` from lot rows.
    If a row arrived with `weight_grams` instead it would KeyError, which is
    the intended failure mode — silent fall-through here would be a real bug."""
    bad_lots = [{"karat": Karat.K18, "weight_grams": Decimal("100.000")}]
    with pytest.raises(KeyError):
        _compute_holdings_from_rows(products=[], coins=[], ounces=[], lots=bad_lots)


# ── Karat outside the reported set blows up loudly ────────────────────────────

def test_unknown_karat_raises():
    """Defensive: if a row ever carries a karat outside K18/K21/K22/K24 the
    function should raise (silent miscount would be worse than a 500)."""
    products = [{"karat": "K9001", "weight_grams": Decimal("1.000")}]
    with pytest.raises((KeyError, ValueError)):
        _compute_holdings_from_rows(products=products, coins=[], ounces=[], lots=[])


# ── Stable karat ordering in output (snapshot JSON depends on this) ───────────

def test_by_karat_is_returned_in_canonical_order():
    holdings = _compute_holdings_from_rows(products=[], coins=[], ounces=[], lots=[])
    assert [b.karat for b in holdings.by_karat] == [Karat.K18, Karat.K21, Karat.K22, Karat.K24]
