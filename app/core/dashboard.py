"""Dashboard computation helpers (Phase A-E).

Each function takes an AsyncSession + a UTC [start, end) window (or an as-of
date) and returns JSON-friendly primitives. Kept out of app/api/reports.py so
each unit is independently testable. Windows are Beirut-local calendar days
(see app/core/daterange).
"""
import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.core.daterange import BEIRUT_TZ, day_range
from app.core.money import money
from app.models import Order, OrderItem, OrderStatus

ZERO = Decimal("0")
_Q_MONEY = Decimal("0.01")
_Q_GRAMS = Decimal("0.001")


def week_window(today: date) -> tuple[datetime, datetime]:
    """UTC [start, end) spanning the 7 Beirut calendar days ending on `today`."""
    start = day_range(today - timedelta(days=6))[0]
    end = day_range(today)[1]
    return start, end


# ── Concurrent fan-out (NEX-53) ───────────────────────────────────────────────
# Two limits, both in Settings:
#   dashboard_max_concurrency — lanes (sessions) ONE load runs at a time;
#   dashboard_max_sessions    — lane sessions ALL loads in this process hold
#                               together. Simultaneous loads queue for lanes
#                               here instead of draining the connection pool
#                               (5 + 10 overflow) from under the till.

_lane_gate: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def lane_gate() -> asyncio.Semaphore:
    """The process-wide cap on dashboard lane sessions, shared by every load.

    Created on first use and kept per event loop, because an asyncio.Semaphore
    belongs to the loop it first waits on; the app runs one loop, so in practice
    this is a single gate for the life of the process."""
    global _lane_gate
    loop = asyncio.get_running_loop()
    if _lane_gate is None or _lane_gate[0] is not loop:
        _lane_gate = (loop, asyncio.Semaphore(settings.dashboard_max_sessions))
    return _lane_gate[1]


@dataclass(frozen=True)
class Windows:
    """Every instant and boundary one dashboard load works from, all derived
    from a single clock read so concurrent sections cannot straddle midnight
    differently. Day windows are Beirut-local, held as UTC [start, end)."""
    now: datetime
    today: date
    today_start: datetime
    today_end: datetime
    week_start: datetime
    week_end: datetime
    prev_week_start: datetime
    prev_week_end: datetime


def windows(now: datetime) -> Windows:
    today = now.astimezone(BEIRUT_TZ).date()
    today_start, today_end = day_range(today)
    week_start, week_end = week_window(today)
    return Windows(
        now=now, today=today, today_start=today_start, today_end=today_end,
        week_start=week_start, week_end=week_end,
        prev_week_start=day_range(today - timedelta(days=13))[0], prev_week_end=week_start,
    )


async def run_sections(session_factory: async_sessionmaker[AsyncSession], sections, w: Windows,
                       *, max_concurrency: int, gate: asyncio.Semaphore) -> list:
    """Run every `section(db, w)` and return the results in `sections` order.

    At most `max_concurrency` lanes run at once. Each lane opens ONE session and
    keeps pulling the next unstarted section onto it, so no session is ever
    shared between concurrent tasks and a load checks out no more connections
    than it has lanes. Read-only — a lane never commits.

    A lane takes a slot from `gate` before it opens its session and holds it
    until the session is closed, so every load sharing that gate is bounded
    together; a lane that had to queue for its slot opens nothing if the work
    is already done.

    The first section to raise fails the whole call (no partial results); the
    other lanes are cancelled and awaited so no query outlives the request."""
    results: list = [None] * len(sections)
    pending = deque(enumerate(sections))

    async def lane() -> None:
        async with gate:
            if not pending:
                return
            async with session_factory() as db:
                while pending:
                    i, section = pending.popleft()
                    results[i] = await section(db, w)

    lanes = [asyncio.ensure_future(lane()) for _ in range(min(max_concurrency, len(sections)))]
    try:
        await asyncio.gather(*lanes)
    except BaseException:
        for task in lanes:
            task.cancel()
        await asyncio.gather(*lanes, return_exceptions=True)
        raise
    return results


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
    # The rate arrives as a float (gold_api): read it through its exact text.
    # Decimal(84.31) is 84.31000000000000227…, enough to tip a half-cent tie.
    r = Decimal(str(rate_24k)) if rate_24k is not None else ZERO
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


# ── Phase B — money pulse ─────────────────────────────────────────────────────

from app.core import ap as ap_core  # noqa: E402
from app.core import ar as ar_core  # noqa: E402
from app.models import GLJournalEntry  # noqa: E402


async def gl_has_entries(db: AsyncSession) -> bool:
    """True once the GL is active (auto-post on + opening balances posted).
    Gates the cash/VAT tiles, which read GL balances (zero while dormant)."""
    return (await db.execute(select(func.count(GLJournalEntry.id)))).scalar_one() > 0


async def receivables(db: AsyncSession, *, as_of: date) -> dict:
    a = await ar_core.compute_aging(db, as_of=as_of)
    t = a["totals"]
    return {"total": money(a["grand_total"]), "b0_30": money(t["0_30"]), "b31_60": money(t["31_60"]),
            "b61_90": money(t["61_90"]), "b90_plus": money(t["90_plus"])}


async def payables_aging(db: AsyncSession, *, as_of: date) -> dict:
    a = await ap_core.compute_ap_aging(db, as_of=as_of)
    c = a["cash_buckets"]
    return {"cash_total": money(a["cash_total"]), "b0_30": money(c["0_30"]), "b31_60": money(c["31_60"]),
            "b61_90": money(c["61_90"]), "b90_plus": money(c["90_plus"]),
            # grams, not money: stays a number
            "metal_owed_by_karat": {k: float(v) for k, v in a["metal_owed_by_karat"].items()}}


async def cash_bank_balance(db: AsyncSession) -> Decimal:
    """USD-base balance across active bank accounts (GL-derived → 0 while dormant).
    Summed in SQL: one round-trip, and the ledger lines stay in the database."""
    from app.models import BankAccount, GLJournalLine
    total = (await db.execute(
        select(func.coalesce(func.sum(GLJournalLine.base_debit - GLJournalLine.base_credit), 0))
        .join(BankAccount, BankAccount.gl_account_id == GLJournalLine.account_id)
        .where(BankAccount.is_active.is_(True))
    )).scalar_one()
    return Decimal(total).quantize(_Q_MONEY)


async def vat_position(db: AsyncSession, today: date) -> dict:
    """Current-quarter VAT (GL-derived). Quarter computed from the Beirut date."""
    from app.core import tax as tax_core
    q = (today.month - 1) // 3 + 1
    vr = await tax_core.compute_vat_return(db, year=today.year, quarter=q)
    return {"net_payable": money(vr["net_payable"]), "direction": vr["direction"],
            "period_label": f"Q{q} {today.year}"}


# ── Phase E — loss-prevention (read-only over the ledger + orders) ────────────

from app.core import ledger as _ledger  # noqa: E402
from app.models import InventoryLedger, Settings  # noqa: E402


async def loss_prevention(db: AsyncSession, start: datetime, end: datetime) -> dict:
    def _count_event(ev):
        return select(func.count(InventoryLedger.id)).where(
            InventoryLedger.event_type == ev,
            InventoryLedger.occurred_at >= start, InventoryLedger.occurred_at < end)
    voids = (await db.execute(_count_event(_ledger.EVENT_ORDER_VOID))).scalar_one()
    overrides = (await db.execute(_count_event(_ledger.EVENT_GOLD_RATE_OVERRIDE_SET))).scalar_one()
    s = (await db.execute(select(Settings).limit(1))).scalar_one_or_none()
    threshold = s.max_discount_percent if s else ZERO
    excess = (await db.execute(select(func.count(Order.id)).where(
        Order.status == OrderStatus.COMPLETED,
        Order.created_at >= start, Order.created_at < end,
        Order.discount_percent > threshold))).scalar_one()
    return {"order_voids": int(voids), "rate_overrides": int(overrides),
            "excess_discount_orders": int(excess)}


# ── Phase C — profitability (from the sale-time cost snapshot) ────────────────

async def profitability(db: AsyncSession, start: datetime, end: datetime) -> dict | None:
    """Gross profit / margin / profit-per-gram over COMPLETED orders in the
    window that have a captured cost basis. Returns None when no cost-captured
    orders exist (go-forward: pre-feature orders are excluded)."""
    rows = (await db.execute(
        select(OrderItem.final_price, OrderItem.cost_basis_usd,
               OrderItem.weight_grams, OrderItem.quantity, Order.created_at)
        .join(Order, OrderItem.order_id == Order.id)
        .where(Order.status == OrderStatus.COMPLETED, Order.created_at >= start, Order.created_at < end,
               OrderItem.cost_basis_usd.is_not(None))
    )).all()
    if not rows:
        return None
    revenue = sum((r.final_price for r in rows), ZERO)
    cost = sum((r.cost_basis_usd for r in rows), ZERO)
    grams = sum((r.weight_grams * r.quantity for r in rows), ZERO)
    gross = revenue - cost
    since = min(r.created_at for r in rows)
    return {
        "gross_profit": gross.quantize(_Q_MONEY),
        "gross_margin_pct": (gross / revenue * 100).quantize(_Q_MONEY) if revenue else None,
        "profit_per_gram": (gross / grams).quantize(_Q_MONEY) if grams else None,
        "since": since.date().isoformat(),
    }
