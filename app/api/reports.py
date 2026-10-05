import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import settings
from app.core import dashboard as dash
from app.core.daterange import day_range
from app.core.gold_api import get_current_gold_rate
from app.core.money import money
from app.core.permissions import require_admin
from app.deps import get_current_user, get_db, get_session_factory
from app.models import (
    CoinType,
    DebtUnit,
    GoldLot,
    GoldRateHistory,
    Order,
    OrderItem,
    OrderStatus,
    OunceType,
    Supplier,
    SupplierBalance,
    SupplierPurchase,
    User,
)

router = APIRouter(prefix="/reports", tags=["reports"])


def _inv_value_money(v: dict) -> dict:
    """Convert the inventory_valuation Decimal fields to money strings for the payload."""
    return {
        "total_usd": money(v["total_usd"]),
        "pure_gold_usd": money(v["pure_gold_usd"]),
        "coins_usd": money(v["coins_usd"]),
        "ounces_usd": money(v["ounces_usd"]),
        "products_usd": money(v["products_usd"]),
        "rate_24k": money(v["rate_24k"]) if v["rate_24k"] is not None else None,
        "method": v["method"],
    }


# ── Dashboard sections ────────────────────────────────────────────────────────
# Each section returns its slice of the payload and runs on a session of its own
# (dash.run_sections), concurrently with the others — so a section may read only
# `w` and its own queries, never another section's result.

async def _sales_totals(db: AsyncSession, w: dash.Windows) -> dict:
    # Today stats (Beirut-local calendar day, half-open window)
    today_orders = (await db.execute(
        select(func.count(Order.id)).where(
            Order.created_at >= w.today_start, Order.created_at < w.today_end,
            Order.status == OrderStatus.COMPLETED
        )
    )).scalar_one()

    today_revenue = (await db.execute(
        select(func.coalesce(func.sum(Order.total_usd), 0)).where(
            Order.created_at >= w.today_start, Order.created_at < w.today_end,
            Order.status == OrderStatus.COMPLETED
        )
    )).scalar_one()

    # This week revenue (7 Beirut days)
    week_revenue = (await db.execute(
        select(func.coalesce(func.sum(Order.total_usd), 0)).where(
            Order.created_at >= w.week_start, Order.created_at < w.week_end,
            Order.status == OrderStatus.COMPLETED
        )
    )).scalar_one()

    # Previous week revenue for delta
    prev_week_revenue = (await db.execute(
        select(func.coalesce(func.sum(Order.total_usd), 0)).where(
            Order.created_at >= w.prev_week_start, Order.created_at < w.prev_week_end,
            Order.status == OrderStatus.COMPLETED,
        )
    )).scalar_one()

    return {
        "today_orders": today_orders,
        "today_revenue": money(today_revenue),
        "week_revenue": money(week_revenue),
        "prev_week_revenue": money(prev_week_revenue),
        "avg_invoice_value_today": money(dash.avg_invoice(today_revenue, today_orders)),
    }


async def _revenue_chart(db: AsyncSession, w: dash.Windows) -> dict:
    # 7-day chart (daily revenue, Beirut-local calendar days)
    chart_data = []
    for i in range(6, -1, -1):
        d = w.today - timedelta(days=i)
        day_start, day_end = day_range(d)
        rev = (await db.execute(
            select(func.coalesce(func.sum(Order.total_usd), 0)).where(
                Order.created_at >= day_start, Order.created_at < day_end,
                Order.status == OrderStatus.COMPLETED,
            )
        )).scalar_one()
        chart_data.append({"date": d.isoformat(), "revenue": money(rev), "is_today": i == 0})
    return {"chart_data": chart_data}


async def _gold_rate_and_valuation(db: AsyncSession, w: dash.Windows) -> dict:
    # Gold rate (+ staleness from the same source the live-rate endpoint uses)
    latest_rate = (await db.execute(
        select(GoldRateHistory.rate_24k).order_by(GoldRateHistory.fetched_at.desc()).limit(1)
    )).scalar_one_or_none()
    rate_info = await get_current_gold_rate(db)
    return {
        "gold_rate_24k": money(latest_rate) if latest_rate else None,
        "gold_rate_is_stale": bool(rate_info["is_stale"]),
        "gold_rate_fetched_at": rate_info["fetched_at"].isoformat() if rate_info.get("fetched_at") else None,
        # Phase D — market valuation, priced at the rate resolved just above
        "inventory_value": _inv_value_money(
            await dash.inventory_valuation(db, rate_24k=rate_info.get("rate"))),
    }


async def _sales_activity(db: AsyncSession, w: dash.Windows) -> dict:
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
        .where(Order.created_at >= w.week_start, Order.status == OrderStatus.COMPLETED)
        .group_by(OrderItem.product_code, OrderItem.product_name, OrderItem.karat)
        .order_by(func.count(OrderItem.id).desc())
        .limit(5)
    )).all()

    # Recent 5 orders
    recent_orders = (await db.execute(
        select(Order)
        .options(selectinload(Order.cashier))
        .order_by(Order.created_at.desc())
        .limit(5)
    )).scalars().all()

    return {
        "top_sellers": [
            {"code": r.product_code, "name": r.product_name, "karat": r.karat, "units": r.units, "revenue": money(r.revenue)}
            for r in top_sellers_rows
        ],
        "recent_orders": [
            {
                "id": o.id,
                "order_number": o.order_number,
                "status": o.status.value,
                "total_usd": money(o.total_usd),
                "cashier": o.cashier.name,
                "created_at": o.created_at.isoformat(),
            }
            for o in recent_orders
        ],
    }


async def _headline_kpis(db: AsyncSession, w: dash.Windows) -> dict:
    # Phase A — jeweler headline KPIs
    weight_today = await dash.gold_weight_sold_by_karat(db, w.today_start, w.today_end)
    weight_week = await dash.gold_weight_sold_by_karat(db, w.week_start, w.week_end)
    making_today = await dash.making_charges(db, w.today_start, w.today_end)
    making_week = await dash.making_charges(db, w.week_start, w.week_end)

    # Phase C — profitability (None until cost-captured orders exist; go-forward)
    _prof = await dash.profitability(db, w.week_start, w.week_end)
    profitability = None if _prof is None else {
        "gross_profit": money(_prof["gross_profit"]),
        "gross_margin_pct": float(_prof["gross_margin_pct"]) if _prof["gross_margin_pct"] is not None else None,
        "profit_per_gram": money(_prof["profit_per_gram"]) if _prof["profit_per_gram"] is not None else None,
        "since": _prof["since"],
    }

    return {
        "gold_weight_sold_today_by_karat": [
            {"karat": r["karat"], "grams": float(r["grams"])} for r in weight_today
        ],
        "gold_weight_sold_week_by_karat": [
            {"karat": r["karat"], "grams": float(r["grams"])} for r in weight_week
        ],
        "making_charges_today": money(making_today),
        "making_charges_week": money(making_week),
        "profitability": profitability,
    }


async def _inventory_pulse(db: AsyncSession, w: dash.Windows) -> dict:
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

    return {
        "inventory": {
            "pure_gold_by_karat": pure_gold_totals,
            "coins": {"on_hand_total": int(coin_total), "distinct_types": int(coin_distinct)},
            "ounces": {"on_hand_total": int(ounce_total), "distinct_types": int(ounce_distinct)},
            "low_stock_alerts": await dash.low_stock_count(db),
        },
    }


async def _inventory_health(db: AsyncSession, w: dash.Windows) -> dict:
    # Phase D — inventory health (aging, dead-stock)
    return {
        "inventory_aging": await dash.inventory_aging(db, asof=w.now),
        "dead_stock_count": await dash.dead_stock_count(db, asof=w.now),
    }


async def _recent_purchases(db: AsyncSession, w: dash.Windows) -> dict:
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

    return {
        "recent_purchases": [
            {
                "id": p.id,
                "supplier": supplier_names.get(p.supplier_id, "—"),
                "occurred_at": p.occurred_at.isoformat(),
                "total_cash_due": money(p.total_cash_due),
                "item_count": len(p.items),
            }
            for p in recent_purchases
        ],
    }


async def _payables(db: AsyncSession, w: dash.Windows) -> dict:
    # Phase B — AP aging, from the supplier subledger (dormant-safe)
    return {"payables_aging": await dash.payables_aging(db, as_of=w.today)}


async def _money_pulse(db: AsyncSession, w: dash.Windows) -> dict:
    # Phase B — money pulse. AR aging comes from the subledger (dormant-safe);
    # cash & VAT read GL balances, so they appear only once the GL is active.
    receivables = await dash.receivables(db, as_of=w.today)
    gl_live = await dash.gl_has_entries(db)
    return {
        "receivables": receivables,
        "cash_bank_balance": money(await dash.cash_bank_balance(db)) if gl_live else None,
        "vat_position": (await dash.vat_position(db, w.today)) if gl_live else None,
    }


async def _loss_prevention(db: AsyncSession, w: dash.Windows) -> dict:
    # Phase E — loss-prevention (last 7 Beirut days)
    return {"loss_prevention": await dash.loss_prevention(db, w.week_start, w.week_end)}


# Heaviest first (by round-trips): lanes pull sections in this order, so the
# long ones start immediately and the short ones fill in behind them.
_SECTIONS = (
    _inventory_pulse,
    _revenue_chart,
    _gold_rate_and_valuation,
    _money_pulse,
    _headline_kpis,
    _sales_totals,
    _payables,
    _loss_prevention,
    _sales_activity,
    _inventory_health,
    _recent_purchases,
)

# The payload's key order, unchanged from when the handler ran every query in
# sequence — it must not depend on the order the sections happen to finish in.
_PAYLOAD_KEYS = (
    "today_orders",
    "today_revenue",
    "week_revenue",
    "prev_week_revenue",
    "gold_rate_24k",
    "gold_rate_is_stale",
    "gold_rate_fetched_at",
    "chart_data",
    "top_sellers",
    # Phase A — jeweler headline KPIs
    "gold_weight_sold_today_by_karat",
    "gold_weight_sold_week_by_karat",
    "avg_invoice_value_today",
    "making_charges_today",
    "making_charges_week",
    "recent_orders",
    # Phase 7 — inventory pulse + AP
    "inventory",
    # Phase D — inventory health (market valuation, aging, dead-stock)
    "inventory_value",
    "inventory_aging",
    "dead_stock_count",
    "recent_purchases",
    # Phase B — money pulse (AP aging replaces the old accounts_payable block)
    "receivables",
    "payables_aging",
    "cash_bank_balance",
    "vat_position",
    # Phase E — loss-prevention (last 7 Beirut days)
    "loss_prevention",
    # Phase C — profitability (null until cost-captured sales exist)
    "profitability",
)


async def build_dashboard(
    session_factory: async_sessionmaker[AsyncSession], *, now: datetime,
    max_concurrency: int | None = None, gate: asyncio.Semaphore | None = None,
) -> dict:
    """Assemble the dashboard payload as of `now`, running the sections
    concurrently on their own sessions. `now` is the load's only clock read:
    every window is derived from it once and shared by all sections.

    `max_concurrency` (default settings.dashboard_max_concurrency) bounds this
    load; `gate` (default the process-wide dash.lane_gate()) bounds all loads."""
    w = dash.windows(now)
    parts: dict = {}
    if max_concurrency is None:
        max_concurrency = settings.dashboard_max_concurrency
    if gate is None:
        gate = dash.lane_gate()
    for part in await dash.run_sections(session_factory, _SECTIONS, w,
                                        max_concurrency=max_concurrency, gate=gate):
        parts.update(part)
    return {key: parts[key] for key in _PAYLOAD_KEYS}


def utc_now() -> datetime:
    """The request's clock read. A dependency so tests can pin the instant."""
    return datetime.now(timezone.utc)


@router.get("/dashboard")
async def dashboard(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    now: datetime = Depends(utc_now),
):
    # The request session has done its job (auth) but would keep its connection
    # until the response is sent. Hand it back first: a load, including one
    # queued for lanes, then holds lane connections only, and those are capped.
    await db.close()
    return await build_dashboard(session_factory, now=now)
