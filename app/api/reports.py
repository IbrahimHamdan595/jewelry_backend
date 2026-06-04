from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import dashboard as dash
from app.core.daterange import BEIRUT_TZ, day_range
from app.core.gold_api import get_current_gold_rate
from app.core.permissions import require_admin
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
    ProductStatus,
    Supplier,
    SupplierBalance,
    SupplierPurchase,
    User,
)

router = APIRouter(prefix="/reports", tags=["reports"])


def _inv_value_floats(v: dict) -> dict:
    """Convert the inventory_valuation Decimal fields to floats for the payload."""
    return {
        "total_usd": float(v["total_usd"]),
        "pure_gold_usd": float(v["pure_gold_usd"]),
        "coins_usd": float(v["coins_usd"]),
        "ounces_usd": float(v["ounces_usd"]),
        "products_usd": float(v["products_usd"]),
        "rate_24k": float(v["rate_24k"]) if v["rate_24k"] is not None else None,
        "method": v["method"],
    }


@router.get("/dashboard")
async def dashboard(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    now = datetime.now(timezone.utc)
    today = datetime.now(BEIRUT_TZ).date()
    today_start, today_end = day_range(today)
    week_start, week_end = dash.week_window(today)
    prev_week_start = day_range(today - timedelta(days=13))[0]
    prev_week_end = week_start

    # Today stats (Beirut-local calendar day, half-open window)
    today_orders = (await db.execute(
        select(func.count(Order.id)).where(
            Order.created_at >= today_start, Order.created_at < today_end,
            Order.status == OrderStatus.COMPLETED
        )
    )).scalar_one()

    today_revenue = (await db.execute(
        select(func.coalesce(func.sum(Order.total_usd), 0)).where(
            Order.created_at >= today_start, Order.created_at < today_end,
            Order.status == OrderStatus.COMPLETED
        )
    )).scalar_one()

    # This week revenue (7 Beirut days)
    week_revenue = (await db.execute(
        select(func.coalesce(func.sum(Order.total_usd), 0)).where(
            Order.created_at >= week_start, Order.created_at < week_end,
            Order.status == OrderStatus.COMPLETED
        )
    )).scalar_one()

    # Previous week revenue for delta
    prev_week_revenue = (await db.execute(
        select(func.coalesce(func.sum(Order.total_usd), 0)).where(
            Order.created_at >= prev_week_start, Order.created_at < prev_week_end,
            Order.status == OrderStatus.COMPLETED,
        )
    )).scalar_one()

    # Gold rate (+ staleness from the same source the live-rate endpoint uses)
    latest_rate = (await db.execute(
        select(GoldRateHistory.rate_24k).order_by(GoldRateHistory.fetched_at.desc()).limit(1)
    )).scalar_one_or_none()
    rate_info = await get_current_gold_rate(db)

    # 7-day chart (daily revenue, Beirut-local calendar days)
    chart_data = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        day_start, day_end = day_range(d)
        rev = (await db.execute(
            select(func.coalesce(func.sum(Order.total_usd), 0)).where(
                Order.created_at >= day_start, Order.created_at < day_end,
                Order.status == OrderStatus.COMPLETED,
            )
        )).scalar_one()
        chart_data.append({"date": d.isoformat(), "revenue": float(rev), "is_today": i == 0})

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
    # Phase 3: products participate in low-stock alerts too.
    low_product = (await db.execute(
        select(func.count(Product.id)).where(
            Product.is_active.is_(True),
            Product.min_stock_qty.is_not(None),
            Product.on_hand_qty <= Product.min_stock_qty,
            Product.status.notin_((ProductStatus.MELTED, ProductStatus.INACTIVE)),
        )
    )).scalar_one()

    # Phase 4: recent supplier purchases (for dashboard receipt links).
    recent_purchases = (await db.execute(
        select(SupplierPurchase)
        .options(selectinload(SupplierPurchase.items))
        .order_by(SupplierPurchase.occurred_at.desc())
        .limit(5)
    )).scalars().all()
    purchase_supplier_ids = {p.supplier_id for p in recent_purchases}
    supplier_names: dict[str, str] = {}
    if purchase_supplier_ids:
        for s in (
            await db.execute(select(Supplier).where(Supplier.id.in_(purchase_supplier_ids)))
        ).scalars():
            supplier_names[s.id] = s.name

    # Phase B — money pulse. AR/AP aging come from the subledgers (dormant-safe);
    # cash & VAT read GL balances, so they appear only once the GL is active.
    receivables = await dash.receivables(db, as_of=today)
    payables_aging = await dash.payables_aging(db, as_of=today)
    gl_live = await dash.gl_has_entries(db)
    cash_bank_balance = float(await dash.cash_bank_balance(db)) if gl_live else None
    vat_position = (await dash.vat_position(db, today)) if gl_live else None

    # Phase C — profitability (None until cost-captured orders exist; go-forward)
    _prof = await dash.profitability(db, week_start, week_end)
    profitability = None if _prof is None else {
        "gross_profit": float(_prof["gross_profit"]),
        "gross_margin_pct": float(_prof["gross_margin_pct"]) if _prof["gross_margin_pct"] is not None else None,
        "profit_per_gram": float(_prof["profit_per_gram"]) if _prof["profit_per_gram"] is not None else None,
        "since": _prof["since"],
    }

    return {
        "today_orders": today_orders,
        "today_revenue": float(today_revenue),
        "week_revenue": float(week_revenue),
        "prev_week_revenue": float(prev_week_revenue),
        "gold_rate_24k": float(latest_rate) if latest_rate else None,
        "gold_rate_is_stale": bool(rate_info["is_stale"]),
        "gold_rate_fetched_at": rate_info["fetched_at"].isoformat() if rate_info.get("fetched_at") else None,
        "chart_data": chart_data,
        "top_sellers": top_sellers,
        # Phase A — jeweler headline KPIs
        "gold_weight_sold_today_by_karat": [
            {"karat": r["karat"], "grams": float(r["grams"])}
            for r in await dash.gold_weight_sold_by_karat(db, today_start, today_end)
        ],
        "gold_weight_sold_week_by_karat": [
            {"karat": r["karat"], "grams": float(r["grams"])}
            for r in await dash.gold_weight_sold_by_karat(db, week_start, week_end)
        ],
        "avg_invoice_value_today": float(dash.avg_invoice(today_revenue, today_orders)),
        "making_charges_today": float(await dash.making_charges(db, today_start, today_end)),
        "making_charges_week": float(await dash.making_charges(db, week_start, week_end)),
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
            "low_stock_alerts": await dash.low_stock_count(db),
        },
        # Phase D — inventory health (market valuation, aging, dead-stock)
        "inventory_value": _inv_value_floats(
            await dash.inventory_valuation(db, rate_24k=rate_info.get("rate"))),
        "inventory_aging": await dash.inventory_aging(db, asof=now),
        "dead_stock_count": await dash.dead_stock_count(db, asof=now),
        "recent_purchases": [
            {
                "id": p.id,
                "supplier": supplier_names.get(p.supplier_id, "—"),
                "occurred_at": p.occurred_at.isoformat(),
                "total_cash_due": float(p.total_cash_due),
                "item_count": len(p.items),
            }
            for p in recent_purchases
        ],
        # Phase B — money pulse (AP aging replaces the old accounts_payable block)
        "receivables": receivables,
        "payables_aging": payables_aging,
        "cash_bank_balance": cash_bank_balance,
        "vat_position": vat_position,
        # Phase E — loss-prevention (last 7 Beirut days)
        "loss_prevention": await dash.loss_prevention(db, week_start, week_end),
        # Phase C — profitability (null until cost-captured sales exist)
        "profitability": profitability,
    }
