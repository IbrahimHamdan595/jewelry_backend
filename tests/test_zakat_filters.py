"""Filter-correctness test for compute_zakatable_holdings().

This is the test that proves the WHERE clauses inside the DB wrapper are
right. The pure-function tests in test_zakat_compute.py prove the math; if
the wrapper's filters are wrong they'd silently sum over the wrong rows and
every pure test would still pass while the user sees an inflated number.

The deal: ONE seeded scenario that exercises every exclusion rule plus the
RESERVED inclusion. If this passes, the four SELECT queries are correctly
scoped. If something regresses (e.g. someone changes is_active vs status, or
weight_grams vs weight_remaining_grams), this fails loudly.
"""
from decimal import Decimal

import pytest

from app.core.zakat import (
    SOURCE_COINS,
    SOURCE_LOTS,
    SOURCE_OUNCES,
    SOURCE_PRODUCTS,
    compute_zakatable_holdings,
)
from app.models import (
    CoinType,
    GoldLot,
    Karat,
    LotSource,
    MarginMode,
    OunceType,
    Product,
    ProductStatus,
    User,
    Role,
)


def _make_user() -> User:
    """Minimal user for FK-bearing rows (none of the zakat queries read users,
    but GoldLotConsumption and other tables FK to users — kept here in case
    the seed grows later)."""
    return User(
        id="test-admin",
        email="t@t.t",
        name="t",
        password_hash="x",
        role=Role.ADMIN,
        is_active=True,
    )


@pytest.mark.asyncio
async def test_filters_exclude_sold_melted_depleted_and_zero_qty(db):
    """The single most important test in the whole feature.

    Seeds inventory that mixes on-hand items with items that should be
    EXCLUDED for various reasons. The asserted totals come from ONLY the
    on-hand items. Any filter regression silently doubles or zeroes some
    karat — both visible in this one test.

    Seed plan:
      Products (K21, 10g each):
        ✓ AVAILABLE          ← include
        ✓ RESERVED           ← include (current rule; see RESERVED TODO in compute_zakatable_holdings)
        ✗ SOLD               ← exclude
        ✗ MELTED             ← exclude
        ✗ INACTIVE           ← exclude

      Coins (K22, 5g per coin):
        ✓ on_hand_qty=4      ← include (20g)
        ✗ on_hand_qty=0      ← exclude
        ✗ on_hand_qty=-1     ← exclude (defensive: shouldn't happen but filter must hold)

      Ounces (K24, 31.104g):
        ✓ on_hand_qty=2      ← include (62.208g)
        ✗ on_hand_qty=0      ← exclude

      Lots:
        ✓ K18, 50g remaining, is_depleted=False        ← include
        ✗ K18, 99g remaining, is_depleted=True         ← exclude (depleted but row still present)
        ✗ K24, 200g ORIGINAL but 0 remaining, !depleted ← include in query (filter is is_depleted),
                                                          contributes 0 grams (correct)

    Expected totals:
      K18:   50.000g  →  37.500 Au   (lot only)
      K21:   20.000g  →  17.500 Au   (AVAILABLE + RESERVED products)
      K22:   20.000g  →  18.340 Au   (coins, qty=4)
      K24:   62.208g  →  62.146 Au   (ounces, qty=2) + 0 from zero-remaining-but-not-depleted lot
      Total Au: 135.486g
    """
    # ── Products ──────────────────────────────────────────────────────────
    db.add_all([
        Product(
            code="P-OK-AVAIL", name_en="ok", category="rings", karat=Karat.K21,
            weight_grams=Decimal("10.000"), margin_percent=Decimal("15"),
            making_charge=Decimal("0"), status=ProductStatus.AVAILABLE, is_active=True,
        ),
        Product(
            code="P-OK-RESERVED", name_en="ok2", category="rings", karat=Karat.K21,
            weight_grams=Decimal("10.000"), margin_percent=Decimal("15"),
            making_charge=Decimal("0"), status=ProductStatus.RESERVED, is_active=True,
        ),
        Product(
            code="P-NO-SOLD", name_en="sold", category="rings", karat=Karat.K21,
            weight_grams=Decimal("999.000"),  # huge weight — proves exclusion
            margin_percent=Decimal("15"), making_charge=Decimal("0"),
            status=ProductStatus.SOLD, is_active=True,
        ),
        Product(
            code="P-NO-MELTED", name_en="melted", category="rings", karat=Karat.K21,
            weight_grams=Decimal("999.000"),
            margin_percent=Decimal("15"), making_charge=Decimal("0"),
            status=ProductStatus.MELTED, is_active=False,
        ),
        Product(
            code="P-NO-INACTIVE", name_en="inactive", category="rings", karat=Karat.K21,
            weight_grams=Decimal("999.000"),
            margin_percent=Decimal("15"), making_charge=Decimal("0"),
            status=ProductStatus.INACTIVE, is_active=False,
        ),
    ])

    # ── Coins ─────────────────────────────────────────────────────────────
    db.add_all([
        CoinType(
            code="C-OK", name_en="ok", karat=Karat.K22, weight_grams=Decimal("5.000"),
            markup_per_gram=Decimal("0"), margin_mode=MarginMode.USD,
            margin_value=Decimal("0"), on_hand_qty=4, is_active=True,
        ),
        CoinType(
            code="C-NO-ZERO", name_en="zero", karat=Karat.K22, weight_grams=Decimal("999.000"),
            markup_per_gram=Decimal("0"), margin_mode=MarginMode.USD,
            margin_value=Decimal("0"), on_hand_qty=0, is_active=True,
        ),
        CoinType(
            code="C-NO-NEGATIVE", name_en="neg", karat=Karat.K22, weight_grams=Decimal("999.000"),
            markup_per_gram=Decimal("0"), margin_mode=MarginMode.USD,
            margin_value=Decimal("0"), on_hand_qty=-1, is_active=True,
        ),
    ])

    # ── Ounces ────────────────────────────────────────────────────────────
    db.add_all([
        OunceType(
            code="O-OK", name_en="ok", karat=Karat.K24, weight_grams=Decimal("31.104"),
            markup_per_gram=Decimal("0"), margin_mode=MarginMode.USD,
            margin_value=Decimal("0"), on_hand_qty=2, is_active=True,
        ),
        OunceType(
            code="O-NO-ZERO", name_en="zero", karat=Karat.K24, weight_grams=Decimal("999.000"),
            markup_per_gram=Decimal("0"), margin_mode=MarginMode.USD,
            margin_value=Decimal("0"), on_hand_qty=0, is_active=True,
        ),
    ])

    # ── Lots ──────────────────────────────────────────────────────────────
    db.add_all([
        GoldLot(
            karat=Karat.K18,
            weight_grams=Decimal("50.000"),
            weight_remaining_grams=Decimal("50.000"),  # ← what the zakat query reads
            source=LotSource.SUPPLIER,
            cost_basis_usd=Decimal("0"),
            is_depleted=False,
        ),
        GoldLot(
            karat=Karat.K18,
            weight_grams=Decimal("99.000"),
            weight_remaining_grams=Decimal("99.000"),
            source=LotSource.SUPPLIER,
            cost_basis_usd=Decimal("0"),
            is_depleted=True,  # ← excluded
        ),
        # Row with zero remaining but not marked depleted — included by the
        # filter, contributes zero. Proves we read weight_remaining_grams not
        # weight_grams (200 would inflate K24 if the wrong column were used).
        GoldLot(
            karat=Karat.K24,
            weight_grams=Decimal("200.000"),
            weight_remaining_grams=Decimal("0.000"),
            source=LotSource.SUPPLIER,
            cost_basis_usd=Decimal("0"),
            is_depleted=False,
        ),
    ])

    await db.flush()

    holdings = await compute_zakatable_holdings(db)
    by_karat = {b.karat: b for b in holdings.by_karat}

    # K18: lot 50g, lot.au = 50 * 0.750 = 37.500
    assert by_karat[Karat.K18].grams_by_source[SOURCE_LOTS] == Decimal("50.000")
    assert by_karat[Karat.K18].grams_by_source[SOURCE_PRODUCTS] == Decimal("0.000")
    assert by_karat[Karat.K18].au_grams == Decimal("37.500")

    # K21: AVAILABLE 10g + RESERVED 10g = 20g; au = 20 * 0.875 = 17.500
    assert by_karat[Karat.K21].grams_by_source[SOURCE_PRODUCTS] == Decimal("20.000")
    assert by_karat[Karat.K21].au_grams == Decimal("17.500")

    # K22: coins 4 * 5g = 20g; au = 20 * 0.917 = 18.340
    assert by_karat[Karat.K22].grams_by_source[SOURCE_COINS] == Decimal("20.000")
    assert by_karat[Karat.K22].au_grams == Decimal("18.340")

    # K24: ounces 2 * 31.104g = 62.208g (+ zero-remaining lot contributes 0)
    assert by_karat[Karat.K24].grams_by_source[SOURCE_OUNCES] == Decimal("62.208")
    assert by_karat[Karat.K24].grams_by_source[SOURCE_LOTS] == Decimal("0.000")
    # 62.208 * 0.999 = 62.145792 → 62.146
    assert by_karat[Karat.K24].au_grams == Decimal("62.146")

    # Grand total: 37.500 + 17.500 + 18.340 + 62.146 = 135.486
    # (Unrounded grand_au keeps full precision; final round happens once.)
    assert holdings.total_au_grams == Decimal("135.486")

    # Defensive: if any of the 999g excluded rows had leaked, this assert
    # would catastrophically fail.
    assert holdings.total_au_grams < Decimal("200.000")
