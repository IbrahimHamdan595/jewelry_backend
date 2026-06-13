from datetime import datetime, time, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CoinType, Karat, Order, OunceType, Product

KARAT_PURITY: dict[str, Decimal] = {
    Karat.K18: Decimal("0.750"),
    Karat.K21: Decimal("0.875"),
    Karat.K22: Decimal("0.917"),
    Karat.K24: Decimal("0.999"),
}

KARAT_LABEL: dict[str, str] = {
    Karat.K18: "18K",
    Karat.K21: "21K",
    Karat.K22: "22K",
    Karat.K24: "24K",
}


def _round(value: Decimal, places: int = 2) -> Decimal:
    return value.quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP)


def calculate_price(
    *,
    rate_24k: Decimal,
    karat: Karat,
    weight_grams: Decimal,
    margin_percent: Decimal,
    making_charge: Decimal,
    karat_markup: Decimal = Decimal("0"),
    stone_value: Decimal = Decimal("0"),
) -> dict[str, Decimal]:
    purity = KARAT_PURITY[karat]
    purity_rate = rate_24k * purity
    effective_rate = purity_rate + karat_markup  # owner-defined per-gram addition
    metal_value = effective_rate * weight_grams
    margin_amount = metal_value * (margin_percent / Decimal(100))
    with_margin = metal_value + margin_amount
    final_price = with_margin + making_charge + stone_value

    return {
        "purity_rate": _round(purity_rate),
        "effective_rate": _round(effective_rate),
        "metal_value": _round(metal_value),
        "margin_amount": _round(margin_amount),
        "stone_value": _round(stone_value),
        "final_price": _round(final_price),
    }


def calculate_unit_price(
    *,
    rate_24k: Decimal,
    weight_grams: Decimal,
    markup_per_gram: Decimal,
    margin_mode: str,
    margin_value: Decimal,
) -> dict[str, Decimal]:
    """Price a coin or ounce-bar unit.

    Formula:  (rate_24k + markup_per_gram) * weight_grams + margin
    where margin is either flat USD (`margin_mode='USD'`) or a percentage
    of metal_value (`margin_mode='PERCENT'`). Markup is signed.
    """
    if margin_mode not in ("USD", "PERCENT"):
        raise ValueError(f"margin_mode must be 'USD' or 'PERCENT', got {margin_mode!r}")

    effective_rate = rate_24k + markup_per_gram
    metal_value = effective_rate * weight_grams
    if margin_mode == "PERCENT":
        margin_amount = metal_value * (margin_value / Decimal(100))
    else:
        margin_amount = margin_value
    final_price = metal_value + margin_amount

    return {
        "effective_rate": _round(effective_rate),
        "metal_value": _round(metal_value),
        "margin_amount": _round(margin_amount),
        "final_price": _round(final_price),
    }


def compute_buyback_price(
    *,
    rate_24k: Decimal,
    karat: Karat,
    weight_grams: Decimal,
    margin_mode: str,
    margin_value: Decimal,
) -> dict[str, Decimal]:
    """Compute the price the shop pays a walk-in seller for `weight_grams` of `karat` gold.

    Formula:
        purity_rate = rate_24k * KARAT_PURITY[karat]
        USD_PER_GRAM: effective = purity_rate - margin_value;     buy = effective * weight
        PERCENT:      effective = purity_rate * (1 - margin/100); buy = effective * weight

    Raises ValueError if margin_mode is unknown, percent margin >= 100,
    or the resulting effective rate is non-positive.
    """
    if margin_mode not in ("USD_PER_GRAM", "PERCENT"):
        raise ValueError(
            f"margin_mode must be 'USD_PER_GRAM' or 'PERCENT', got {margin_mode!r}"
        )
    if margin_mode == "PERCENT" and margin_value >= Decimal(100):
        raise ValueError(f"PERCENT margin must be < 100, got {margin_value}")

    purity_rate = rate_24k * KARAT_PURITY[karat]
    if margin_mode == "USD_PER_GRAM":
        effective = purity_rate - margin_value
    else:
        effective = purity_rate * (Decimal(1) - margin_value / Decimal(100))

    if effective <= 0:
        raise ValueError(
            f"Buyback margin produces a non-positive rate (purity_rate={purity_rate}, "
            f"effective={effective}). Refusing to compute."
        )

    buy_price = effective * weight_grams

    return {
        "purity_rate": _round(purity_rate),
        "effective_rate_per_gram": _round(effective),
        "buy_price": _round(buy_price),
    }


async def generate_order_number(db: AsyncSession, when: datetime) -> str:
    yyyymmdd = when.strftime("%Y%m%d")
    day_start = datetime.combine(when.date(), time.min, tzinfo=timezone.utc)
    day_end = datetime.combine(when.date(), time.max, tzinfo=timezone.utc)
    result = await db.execute(
        select(func.count(Order.id)).where(Order.created_at.between(day_start, day_end))
    )
    today_count = result.scalar_one()
    return f"ORD-{yyyymmdd}-{(today_count + 1):03d}"


async def generate_item_code(db: AsyncSession, karat: Karat) -> str:
    # Store SKU prefix. Historically "MZB" (placeholder brand); new items use
    # "FN" (Fawaz El Namel). Existing MZB-* codes are intentionally left as-is —
    # changing the prefix only affects newly generated codes, and numbering is
    # per-prefix (startswith), so FN-* starts fresh at 0001.
    prefix = f"FN-{KARAT_LABEL[karat]}-"
    result = await db.execute(
        select(Product.code)
        .where(Product.code.startswith(prefix))
        .order_by(Product.code.desc())
        .limit(1)
    )
    last_code = result.scalar_one_or_none()
    last_num = int(last_code.split("-")[2]) if last_code else 0
    return f"{prefix}{(last_num + 1):04d}"


async def generate_unit_code(db: AsyncSession, kind: str, karat: Karat) -> str:
    """Auto-generate a code for a coin_type or ounce_type.

    Format: FN-COIN-{karat}-NNNN or FN-OZ-{karat}-NNNN. Numbering is per
    (kind, karat) — so K22 coins and K24 coins each have their own counter.
    (Legacy items use the old "MZB" prefix; those are left untouched.)
    """
    if kind == "COIN":
        prefix = f"FN-COIN-{KARAT_LABEL[karat]}-"
        Model = CoinType
    elif kind == "OUNCE":
        prefix = f"FN-OZ-{KARAT_LABEL[karat]}-"
        Model = OunceType
    else:
        raise ValueError(f"unknown unit kind {kind!r}")

    result = await db.execute(
        select(Model.code)
        .where(Model.code.startswith(prefix))
        .order_by(Model.code.desc())
        .limit(1)
    )
    last_code = result.scalar_one_or_none()
    # Codes like FN-COIN-22K-0042 → rsplit → "0042" (works for legacy MZB-* too)
    last_num = int(last_code.rsplit("-", 1)[-1]) if last_code else 0
    return f"{prefix}{(last_num + 1):04d}"
