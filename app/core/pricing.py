from datetime import datetime, time, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Karat, Product, Order

KARAT_PURITY: dict[str, Decimal] = {
    Karat.K18: Decimal("0.750"),
    Karat.K21: Decimal("0.875"),
    Karat.K24: Decimal("0.999"),
}

KARAT_LABEL: dict[str, str] = {
    Karat.K18: "18K",
    Karat.K21: "21K",
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
) -> dict[str, Decimal]:
    purity = KARAT_PURITY[karat]
    purity_rate = rate_24k * purity
    effective_rate = purity_rate + karat_markup  # owner-defined per-gram addition
    metal_value = effective_rate * weight_grams
    margin_amount = metal_value * (margin_percent / Decimal(100))
    with_margin = metal_value + margin_amount
    final_price = with_margin + making_charge

    return {
        "purity_rate": _round(purity_rate),
        "effective_rate": _round(effective_rate),
        "metal_value": _round(metal_value),
        "margin_amount": _round(margin_amount),
        "final_price": _round(final_price),
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
    prefix = f"MZB-{KARAT_LABEL[karat]}-"
    result = await db.execute(
        select(Product.code)
        .where(Product.code.startswith(prefix))
        .order_by(Product.code.desc())
        .limit(1)
    )
    last_code = result.scalar_one_or_none()
    last_num = int(last_code.split("-")[2]) if last_code else 0
    return f"{prefix}{(last_num + 1):04d}"
