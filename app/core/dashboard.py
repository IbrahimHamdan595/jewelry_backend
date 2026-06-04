"""Dashboard computation helpers (Phase A-E).

Each function takes an AsyncSession + a UTC [start, end) window (or an as-of
date) and returns JSON-friendly primitives. Kept out of app/api/reports.py so
each unit is independently testable. Windows are Beirut-local calendar days
(see app/core/daterange).
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.daterange import BEIRUT_TZ, day_range
from app.models import Order, OrderItem, OrderStatus

ZERO = Decimal("0")
_Q_MONEY = Decimal("0.01")
_Q_GRAMS = Decimal("0.001")


def week_window(today: date) -> tuple[datetime, datetime]:
    """UTC [start, end) spanning the 7 Beirut calendar days ending on `today`."""
    start = day_range(today - timedelta(days=6))[0]
    end = day_range(today)[1]
    return start, end


# ── Phase A — headline KPIs ───────────────────────────────────────────────────

async def gold_weight_sold_by_karat(db: AsyncSession, start: datetime, end: datetime) -> list[dict]:
    rows = (await db.execute(
        select(OrderItem.karat, func.coalesce(func.sum(OrderItem.weight_grams * OrderItem.quantity), 0))
        .join(Order, OrderItem.order_id == Order.id)
        .where(Order.status == OrderStatus.COMPLETED, Order.created_at >= start, Order.created_at < end)
        .group_by(OrderItem.karat)
    )).all()
    return [{"karat": (k.value if hasattr(k, "value") else str(k)),
             "grams": Decimal(g).quantize(_Q_GRAMS)} for k, g in rows]


async def making_charges(db: AsyncSession, start: datetime, end: datetime) -> Decimal:
    total = (await db.execute(
        select(func.coalesce(func.sum(OrderItem.making_charge * OrderItem.quantity), 0))
        .join(Order, OrderItem.order_id == Order.id)
        .where(Order.status == OrderStatus.COMPLETED, Order.created_at >= start, Order.created_at < end)
    )).scalar_one()
    return Decimal(total).quantize(_Q_MONEY)


def avg_invoice(today_revenue: Decimal, today_orders: int) -> Decimal:
    if not today_orders:
        return Decimal("0").quantize(_Q_MONEY)
    return (Decimal(today_revenue) / today_orders).quantize(_Q_MONEY)


# ── Phase D — inventory health ────────────────────────────────────────────────

from app.core.pricing import KARAT_PURITY  # noqa: E402
from app.models import (  # noqa: E402
    CoinType, GoldLot, OunceType, Product, ProductStatus,
)

_DEAD_STATUSES = (ProductStatus.MELTED, ProductStatus.INACTIVE)


async def inventory_valuation(db: AsyncSession, *, rate_24k: Decimal | None) -> dict:
    """Total on-hand inventory value in USD at the live 24K rate (market method).
    Coins/ounces have no cost basis, so everything is valued at market; products
    use their cost_basis_usd when present, else the market proxy."""
    r = Decimal(rate_24k) if rate_24k is not None else ZERO
    lots = (await db.execute(select(GoldLot).where(GoldLot.is_depleted.is_(False)))).scalars().all()
    pure = sum((l.weight_remaining_grams * KARAT_PURITY[l.karat] * r for l in lots), ZERO)
    coins = (await db.execute(select(CoinType).where(CoinType.is_active.is_(True)))).scalars().all()
    coins_usd = sum((c.on_hand_qty * c.weight_grams * KARAT_PURITY[c.karat] * r for c in coins), ZERO)
    ounces = (await db.execute(select(OunceType).where(OunceType.is_active.is_(True)))).scalars().all()
    ounces_usd = sum((o.on_hand_qty * o.weight_grams * KARAT_PURITY[o.karat] * r for o in ounces), ZERO)
    prods = (await db.execute(select(Product).where(
        Product.is_active.is_(True), Product.status.notin_(_DEAD_STATUSES)))).scalars().all()
    prod_usd = ZERO
    for p in prods:
        unit = p.cost_basis_usd if p.cost_basis_usd is not None else (p.weight_grams * KARAT_PURITY[p.karat] * r)
        prod_usd += p.on_hand_qty * unit
    q = lambda v: Decimal(v).quantize(_Q_MONEY)  # noqa: E731
    return {"pure_gold_usd": q(pure), "coins_usd": q(coins_usd), "ounces_usd": q(ounces_usd),
            "products_usd": q(prod_usd), "total_usd": q(pure + coins_usd + ounces_usd + prod_usd),
            "rate_24k": q(r) if rate_24k is not None else None, "method": "market"}


def _age_bucket(buckets: dict, asof: datetime, dt) -> None:
    if dt is None:
        return
    if dt.tzinfo is None:  # SQLite returns naive; treat stored instants as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    days = (asof - dt).days
    if days <= 90:
        buckets["d0_90"] += 1
    elif days <= 180:
        buckets["d90_180"] += 1
    elif days <= 365:
        buckets["d180_365"] += 1
    else:
        buckets["d365_plus"] += 1


async def inventory_aging(db: AsyncSession, *, asof: datetime) -> dict:
    buckets = {"d0_90": 0, "d90_180": 0, "d180_365": 0, "d365_plus": 0}
    for l in (await db.execute(select(GoldLot).where(GoldLot.is_depleted.is_(False)))).scalars():
        _age_bucket(buckets, asof, l.acquired_at)
    for p in (await db.execute(select(Product).where(
            Product.is_active.is_(True), Product.status.notin_(_DEAD_STATUSES),
            Product.on_hand_qty > 0))).scalars():
        _age_bucket(buckets, asof, p.created_at)
    return buckets


async def low_stock_count(db: AsyncSession) -> int:
    low_coin = (await db.execute(select(func.count(CoinType.id)).where(
        CoinType.is_active.is_(True), CoinType.min_stock_qty.is_not(None),
        CoinType.on_hand_qty <= CoinType.min_stock_qty))).scalar_one()
    low_ounce = (await db.execute(select(func.count(OunceType.id)).where(
        OunceType.is_active.is_(True), OunceType.min_stock_qty.is_not(None),
        OunceType.on_hand_qty <= OunceType.min_stock_qty))).scalar_one()
    low_prod = (await db.execute(select(func.count(Product.id)).where(
        Product.is_active.is_(True), Product.min_stock_qty.is_not(None),
        Product.on_hand_qty <= Product.min_stock_qty,
        Product.status.notin_(_DEAD_STATUSES)))).scalar_one()
    return int(low_coin + low_ounce + low_prod)


async def dead_stock_count(db: AsyncSession, *, asof: datetime) -> int:
    """In-stock products that have aged past a year without selling (the useful
    'dead stock' signal — not items already sold/melted)."""
    cutoff = asof - timedelta(days=365)
    n = (await db.execute(select(func.count(Product.id)).where(
        Product.is_active.is_(True), Product.status.notin_(_DEAD_STATUSES),
        Product.on_hand_qty > 0, Product.created_at < cutoff))).scalar_one()
    return int(n)
