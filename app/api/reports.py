from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.deps import get_current_user, get_db
from app.models import (
    CoinType,
    DebtUnit,
    GoldLot,
    GoldRateHistory,
    Order,
    OrderItem,
    OrderStatus,
    OunceType,
    Product,
    Supplier,
    SupplierBalance,
    User,
)

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

    # ── Inventory rollups (Phase 7) ─────────────────────────────────────────
    # Pure-gold per-karat totals (active lots only)
    lot_rows = (await db.execute(
        select(
            GoldLot.karat,
            func.coalesce(func.sum(GoldLot.weight_remaining_grams), 0).label("grams"),
            func.count(GoldLot.id).label("lots"),
        )
        .where(GoldLot.is_depleted.is_(False))
        .group_by(GoldLot.karat)
    )).all()
    pure_gold_totals = [
        {
            "karat": (r.karat.value if hasattr(r.karat, "value") else str(r.karat)),
            "grams_remaining": float(r.grams),
            "lot_count": int(r.lots),
        }
        for r in lot_rows
    ]

    # Coin / ounce stock totals (active types only)
    coin_total = (await db.execute(
        select(func.coalesce(func.sum(CoinType.on_hand_qty), 0))
        .where(CoinType.is_active.is_(True))
    )).scalar_one()
    coin_distinct = (await db.execute(
        select(func.count(CoinType.id)).where(CoinType.is_active.is_(True))
    )).scalar_one()
    ounce_total = (await db.execute(
        select(func.coalesce(func.sum(OunceType.on_hand_qty), 0))
        .where(OunceType.is_active.is_(True))
    )).scalar_one()
    ounce_distinct = (await db.execute(
        select(func.count(OunceType.id)).where(OunceType.is_active.is_(True))
    )).scalar_one()

    # Low-stock alert count
    low_coin = (await db.execute(
        select(func.count(CoinType.id)).where(
            CoinType.is_active.is_(True),
            CoinType.min_stock_qty.is_not(None),
            CoinType.on_hand_qty <= CoinType.min_stock_qty,
        )
    )).scalar_one()
    low_ounce = (await db.execute(
        select(func.count(OunceType.id)).where(
            OunceType.is_active.is_(True),
            OunceType.min_stock_qty.is_not(None),
            OunceType.on_hand_qty <= OunceType.min_stock_qty,
        )
    )).scalar_one()

    # Accounts payable rollup
    ap_rows = (await db.execute(
        select(SupplierBalance).where(SupplierBalance.balance != 0)
    )).scalars().all()
    ap_cash = Decimal("0")
    ap_gold_by_karat: dict[str, Decimal] = {}
    ap_supplier_ids: set[str] = set()
    for b in ap_rows:
        ap_supplier_ids.add(b.supplier_id)
        if b.unit == DebtUnit.CASH:
            ap_cash += b.balance
        else:
            ap_gold_by_karat[b.karat] = ap_gold_by_karat.get(b.karat, Decimal("0")) + b.balance

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
        # Phase 7 — inventory pulse + AP
        "inventory": {
            "pure_gold_by_karat": pure_gold_totals,
            "coins": {"on_hand_total": int(coin_total), "distinct_types": int(coin_distinct)},
            "ounces": {"on_hand_total": int(ounce_total), "distinct_types": int(ounce_distinct)},
            "low_stock_alerts": int(low_coin + low_ounce),
        },
        "accounts_payable": {
            "total_cash_owed": float(ap_cash),
            "total_grams_owed_by_karat": {k: float(v) for k, v in ap_gold_by_karat.items()},
            "supplier_count": len(ap_supplier_ids),
        },
    }
