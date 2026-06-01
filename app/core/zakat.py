"""Zakat computation service.

THIS MODULE IS THE GROSS-VS-NET SWITCHPOINT.

`compute_zakatable_holdings()` defines what counts toward the total Au.
Today: gross — every gram of gold the shop holds physically is included.
Future (pending religious ruling): net of supplier gold debt. When that lands,
edit ONLY `compute_zakatable_holdings()` — subtract per-karat grams owed from
`SupplierBalance` where `unit == DebtUnit.GOLD`, clamping to >= 0. The API,
snapshot model, and screen do not change.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.gold_api import get_current_gold_rate
from app.core.pricing import KARAT_PURITY
from app.models import (
    CoinType,
    GoldLot,
    Karat,
    OunceType,
    Product,
    ProductStatus,
    Settings,
)

# The canonical zakat rate: 2.5% of zakatable holdings. Single source of
# truth — reuse this constant everywhere zakat is computed.
ZAKAT_RATE: Decimal = Decimal("0.025")

# Ordered list of karats we report in breakdowns. Anything not in this list
# would surface as a KeyError in `_compute_holdings_from_rows`, which is the
# desired behavior — silent fall-through would inflate or deflate the total.
_REPORTED_KARATS: tuple[Karat, ...] = (Karat.K18, Karat.K21, Karat.K22, Karat.K24)

# Per-source keys used inside KaratBucket.grams_by_source. Stable strings so
# the snapshot JSON column is queryable downstream.
SOURCE_PRODUCTS = "products"
SOURCE_COINS = "coins"
SOURCE_OUNCES = "ounces"
SOURCE_LOTS = "lots"
_SOURCES: tuple[str, ...] = (SOURCE_PRODUCTS, SOURCE_COINS, SOURCE_OUNCES, SOURCE_LOTS)


# ── Value objects ─────────────────────────────────────────────────────────────

@dataclass
class KaratBucket:
    karat: Karat
    grams_by_source: dict[str, Decimal]
    total_weight_grams: Decimal  # sum of grams_by_source values
    au_grams: Decimal            # total_weight_grams * KARAT_PURITY[karat]


@dataclass
class ZakatHoldings:
    by_karat: list[KaratBucket]
    total_au_grams: Decimal


@dataclass
class ZakatSummary:
    holdings: ZakatHoldings
    gold_rate_24k: Decimal
    gold_rate_source: str
    gold_rate_is_stale: bool
    gold_rate_fetched_at: datetime
    nisab_grams: Decimal
    meets_nisab: bool
    total_au_value_usd: Decimal
    zakat_au_grams: Decimal
    zakat_value_usd: Decimal


# ── Pure helpers (no DB) ──────────────────────────────────────────────────────

def _round_grams(value: Decimal) -> Decimal:
    """3 decimal places, matching the existing Numeric(10,3) schema."""
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def _round_money(value: Decimal) -> Decimal:
    """2 decimal places, ROUND_HALF_UP — matches app/core/pricing.py."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _empty_grams_by_source() -> dict[str, Decimal]:
    return {src: Decimal("0") for src in _SOURCES}


# Fields that participate in the integrity hash. Order is fixed — changing it
# would invalidate every existing snapshot's hash. If you genuinely need to
# add a new field to the integrity calculation, bump a hash version prefix
# and migrate stored hashes; do not silently reorder.
_INTEGRITY_FIELDS: tuple[str, ...] = (
    "assessment_date",
    "gold_rate_24k_usd_per_gram",
    "gold_rate_source",
    "nisab_grams_used",
    "total_au_grams",
    "total_au_value_usd",
    "zakat_au_grams",
    "zakat_value_usd",
    "meets_nisab",
    "breakdown_by_karat",
)


def _canonical(value):
    """Stable, order-deterministic serialization for hashing.

    Decimals → str (preserves trailing zeros that quantize() set).
    Dates / datetimes → ISO string.
    Dicts → sorted by key recursively.
    Everything else falls through to default json behavior.
    """
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _canonical(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def compute_integrity_hash(snapshot_fields: dict) -> str:
    """sha256 over a canonical JSON dump of the integrity-relevant fields.

    Pure function. Order of dict keys, Decimal precision, and date formatting
    are all normalized so two equivalent snapshots produce the same hash
    regardless of insertion order or Python representation.
    """
    payload = {f: _canonical(snapshot_fields[f]) for f in _INTEGRITY_FIELDS}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _compute_holdings_from_rows(
    *,
    products: Iterable[dict],   # {"karat": Karat, "weight_grams": Decimal}
    coins: Iterable[dict],      # {"karat": Karat, "weight_grams": Decimal, "on_hand_qty": int}
    ounces: Iterable[dict],     # {"karat": Karat, "weight_grams": Decimal, "on_hand_qty": int}
    lots: Iterable[dict],       # {"karat": Karat, "weight_remaining_grams": Decimal}
) -> ZakatHoldings:
    """Compute per-karat Au breakdown from already-filtered row dicts.

    Pure function — no DB, no clock, no live rate. The filtering (which rows
    are 'still on hand') is the caller's responsibility; see
    `compute_zakatable_holdings()` for the production filters.

    Rounds *only* the per-bucket totals and the grand total at the boundary.
    Intermediate sums stay full-precision.
    """
    buckets: dict[Karat, dict[str, Decimal]] = {
        k: _empty_grams_by_source() for k in _REPORTED_KARATS
    }

    for row in products:
        # Phase 3: products are stocked-by-quantity. on_hand_qty defaults to 1
        # so pre-Phase-3 callers (and qty=1 products) are unaffected.
        qty = int(row.get("on_hand_qty", 1))
        buckets[row["karat"]][SOURCE_PRODUCTS] += Decimal(row["weight_grams"]) * qty

    for row in coins:
        qty = int(row["on_hand_qty"])
        buckets[row["karat"]][SOURCE_COINS] += Decimal(row["weight_grams"]) * qty

    for row in ounces:
        qty = int(row["on_hand_qty"])
        buckets[row["karat"]][SOURCE_OUNCES] += Decimal(row["weight_grams"]) * qty

    for row in lots:
        buckets[row["karat"]][SOURCE_LOTS] += Decimal(row["weight_remaining_grams"])

    by_karat: list[KaratBucket] = []
    grand_au = Decimal("0")
    for karat in _REPORTED_KARATS:
        grams_by_source = buckets[karat]
        total_weight = sum(grams_by_source.values(), Decimal("0"))
        au = total_weight * KARAT_PURITY[karat]
        grand_au += au
        by_karat.append(
            KaratBucket(
                karat=karat,
                grams_by_source={k: _round_grams(v) for k, v in grams_by_source.items()},
                total_weight_grams=_round_grams(total_weight),
                au_grams=_round_grams(au),
            )
        )

    return ZakatHoldings(by_karat=by_karat, total_au_grams=_round_grams(grand_au))


# ── DB-touching wrappers ──────────────────────────────────────────────────────

async def compute_zakatable_holdings(db: AsyncSession) -> ZakatHoldings:
    """Query each inventory type with the production 'still on hand' filters,
    then delegate to the pure aggregator.

    GROSS vs NET switchpoint — see module docstring. To switch to net-of-debt:
      • after building the holdings, subtract `SupplierBalance` (unit=GOLD)
        grams per karat from the corresponding bucket, clamping to >= 0;
      • recompute `au_grams` and `total_au_grams` from the adjusted buckets.
    Nothing else in the module/API/UI needs to change.

    NOTE on RESERVED: `ProductStatus.RESERVED` is included alongside AVAILABLE.
    As of 2026-05-24 no code path actually writes RESERVED — the value is a
    forward-looking placeholder. If a reserve flow (e.g. "paid, awaiting
    pickup") is added later, reconsider whether reserved items still belong in
    zakatable holdings; they may have economically transferred ownership even
    while still physically present in the shop.
    """
    product_rows = (
        await db.execute(
            select(Product.karat, Product.weight_grams, Product.on_hand_qty).where(
                Product.status.in_((ProductStatus.AVAILABLE, ProductStatus.RESERVED)),
                Product.on_hand_qty > 0,
            )
        )
    ).all()

    coin_rows = (
        await db.execute(
            select(CoinType.karat, CoinType.weight_grams, CoinType.on_hand_qty).where(
                CoinType.on_hand_qty > 0
            )
        )
    ).all()

    ounce_rows = (
        await db.execute(
            select(OunceType.karat, OunceType.weight_grams, OunceType.on_hand_qty).where(
                OunceType.on_hand_qty > 0
            )
        )
    ).all()

    lot_rows = (
        await db.execute(
            select(GoldLot.karat, GoldLot.weight_remaining_grams).where(
                GoldLot.is_depleted.is_(False)
            )
        )
    ).all()

    return _compute_holdings_from_rows(
        products=(
            {"karat": r.karat, "weight_grams": r.weight_grams, "on_hand_qty": r.on_hand_qty}
            for r in product_rows
        ),
        coins=(
            {"karat": r.karat, "weight_grams": r.weight_grams, "on_hand_qty": r.on_hand_qty}
            for r in coin_rows
        ),
        ounces=(
            {"karat": r.karat, "weight_grams": r.weight_grams, "on_hand_qty": r.on_hand_qty}
            for r in ounce_rows
        ),
        lots=(
            {"karat": r.karat, "weight_remaining_grams": r.weight_remaining_grams}
            for r in lot_rows
        ),
    )


async def compute_zakat_summary(db: AsyncSession) -> ZakatSummary:
    """Live summary: holdings + cash valuation + nisab comparison.

    Reuses `get_current_gold_rate(db)` exactly as the rest of the app does —
    same override → history → stale-flag semantics. Raises `RuntimeError` if
    no rate is available at all (propagate as HTTP 503 at the API layer).
    """
    rate_info = await get_current_gold_rate(db)
    rate_24k = Decimal(str(rate_info["rate"]))

    settings_row = (
        await db.execute(select(Settings).where(Settings.id == "singleton"))
    ).scalar_one()
    nisab_grams = settings_row.nisab_grams

    holdings = await compute_zakatable_holdings(db)

    total_au_value_usd = _round_money(holdings.total_au_grams * rate_24k)
    zakat_au_grams = _round_grams(holdings.total_au_grams * ZAKAT_RATE)
    zakat_value_usd = _round_money(zakat_au_grams * rate_24k)

    return ZakatSummary(
        holdings=holdings,
        gold_rate_24k=rate_24k,
        gold_rate_source=rate_info["source"],
        gold_rate_is_stale=bool(rate_info["is_stale"]),
        gold_rate_fetched_at=rate_info["fetched_at"],
        nisab_grams=nisab_grams,
        meets_nisab=holdings.total_au_grams >= nisab_grams,
        total_au_value_usd=total_au_value_usd,
        zakat_au_grams=zakat_au_grams,
        zakat_value_usd=zakat_value_usd,
    )
