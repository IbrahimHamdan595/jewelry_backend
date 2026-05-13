from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.deps import get_current_user, get_db
from app.models import GoldRateHistory, Order, OrderItem, OrderStatus, Product, User

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/dashboard")
async def dashboard(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = now - timedelta(days=7)
    prev_week_start = week_start - timedelta(days=7)

    # Today stats
    today_orders = (await db.execute(
        select(func.count(Order.id)).where(
            Order.created_at >= today_start, Order.status == OrderStatus.COMPLETED
        )
    )).scalar_one()

    today_revenue = (await db.execute(
        select(func.coalesce(func.sum(Order.total_usd), 0)).where(
            Order.created_at >= today_start, Order.status == OrderStatus.COMPLETED
        )
    )).scalar_one()

    # This week revenue
    week_revenue = (await db.execute(
        select(func.coalesce(func.sum(Order.total_usd), 0)).where(
            Order.created_at >= week_start, Order.status == OrderStatus.COMPLETED
        )
    )).scalar_one()

    # Previous week revenue for delta
    prev_week_revenue = (await db.execute(
        select(func.coalesce(func.sum(Order.total_usd), 0)).where(
            Order.created_at.between(prev_week_start, week_start),
            Order.status == OrderStatus.COMPLETED,
        )
    )).scalar_one()

    # Gold rate
    latest_rate = (await db.execute(
        select(GoldRateHistory.rate_24k).order_by(GoldRateHistory.fetched_at.desc()).limit(1)
    )).scalar_one_or_none()

    # 7-day chart (daily revenue)
    chart_data = []
    for i in range(6, -1, -1):
        day_start = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start.replace(hour=23, minute=59, second=59)
        rev = (await db.execute(
            select(func.coalesce(func.sum(Order.total_usd), 0)).where(
                Order.created_at.between(day_start, day_end),
                Order.status == OrderStatus.COMPLETED,
            )
        )).scalar_one()
        chart_data.append({"date": day_start.date().isoformat(), "revenue": float(rev), "is_today": i == 0})

    # Top sellers this week
    top_sellers_rows = (await db.execute(
        select(
            OrderItem.product_code,
            OrderItem.product_name,
            OrderItem.karat,
            func.count(OrderItem.id).label("units"),
            func.sum(OrderItem.final_price).label("revenue"),
        )
        .join(Order)
        .where(Order.created_at >= week_start, Order.status == OrderStatus.COMPLETED)
        .group_by(OrderItem.product_code, OrderItem.product_name, OrderItem.karat)
        .order_by(func.count(OrderItem.id).desc())
        .limit(5)
    )).all()

    top_sellers = [
        {"code": r.product_code, "name": r.product_name, "karat": r.karat, "units": r.units, "revenue": float(r.revenue)}
        for r in top_sellers_rows
    ]

    # Recent 5 orders
    recent_orders = (await db.execute(
        select(Order)
        .options(selectinload(Order.cashier))
        .order_by(Order.created_at.desc())
        .limit(5)
    )).scalars().all()

    return {
        "today_orders": today_orders,
        "today_revenue": float(today_revenue),
        "week_revenue": float(week_revenue),
        "prev_week_revenue": float(prev_week_revenue),
        "gold_rate_24k": float(latest_rate) if latest_rate else None,
        "chart_data": chart_data,
        "top_sellers": top_sellers,
        "recent_orders": [
            {
                "id": o.id,
                "order_number": o.order_number,
                "status": o.status.value,
                "total_usd": float(o.total_usd),
                "cashier": o.cashier.name,
                "created_at": o.created_at.isoformat(),
            }
            for o in recent_orders
        ],
    }
