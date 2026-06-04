"""Dashboard computation helpers (Phase A-E).

Each function takes an AsyncSession + a UTC [start, end) window (or an as-of
date) and returns JSON-friendly primitives. Kept out of app/api/reports.py so
each unit is independently testable. Windows are Beirut-local calendar days
(see app/core/daterange).
"""
from datetime import date, datetime, timedelta
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
