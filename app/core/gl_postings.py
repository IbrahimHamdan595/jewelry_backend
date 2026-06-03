"""Auto-posting bridge (Module 1): operation events → balanced GL entries.

Each `post_*` mapper builds gl.GLLine lists and calls gl.post_entry inside the
caller's transaction (no commit). All mappers are gated by the
`accounting_auto_post_enabled` settings flag and are idempotent on
(source_type, source_id). See the design spec for the posting catalog.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import gl
from app.core.pricing import KARAT_PURITY
from app.models import (
    DebtUnit, GLAccount, GLJournalEntry, GLPeriod, OrderItemKind, PaymentMethod,
    PeriodStatus, Product, Settings, SupplierItemKind,
)

ZERO = Decimal("0")
_Q_MONEY = Decimal("0.01")

# source_type strings stored on gl_journal_entry.source_type
SOURCE_ORDER = "ORDER"
SOURCE_ORDER_REFUND = "ORDER_REFUND"
SOURCE_SUPPLIER_PURCHASE = "SUPPLIER_PURCHASE"
SOURCE_SUPPLIER_PAYMENT = "SUPPLIER_PAYMENT"
SOURCE_BUYBACK = "BUYBACK"
SOURCE_MELT = "MELT"
SOURCE_ADJUSTMENT = "ADJUSTMENT"


def auto_post_enabled(settings: Settings) -> bool:
    return bool(getattr(settings, "accounting_auto_post_enabled", False))


async def ensure_period(db: AsyncSession, entry_date: date) -> GLPeriod:
    """Return the month's period; create it OPEN if missing. A CLOSED period is
    returned as-is (gl.post_entry will then hard-fail, which is intended)."""
    period = (
        await db.execute(
            select(GLPeriod).where(
                GLPeriod.year == entry_date.year,
                GLPeriod.period_no == entry_date.month,
            )
        )
    ).scalar_one_or_none()
    if period is None:
        period = GLPeriod(year=entry_date.year, period_no=entry_date.month, status=PeriodStatus.OPEN)
        db.add(period)
        await db.flush()
    return period


async def resolve_account_id(db: AsyncSession, system_key: str) -> str:
    acct = (
        await db.execute(select(GLAccount).where(GLAccount.system_key == system_key))
    ).scalar_one_or_none()
    if acct is None:
        raise HTTPException(status_code=422, detail=f"GL system account {system_key} not seeded")
    return acct.id


async def find_live_entry(db: AsyncSession, source_type: str, source_id: str) -> GLJournalEntry | None:
    """The existing forward (non-reversal) entry for a source, if any. Used for
    idempotency (skip double-posts) and to locate the original to reverse."""
    return (
        await db.execute(
            select(GLJournalEntry).where(
                GLJournalEntry.source_type == source_type,
                GLJournalEntry.source_id == source_id,
                GLJournalEntry.reverses_entry_id.is_(None),
            )
        )
    ).scalars().first()


async def _cogs_cost_for_item(db: AsyncSession, item) -> Decimal:
    """Tracked product cost when present, else gold_rate_at_sale × pure-grams proxy."""
    if item.item_kind == OrderItemKind.PRODUCT and item.product_id:
        prod = (await db.execute(select(Product).where(Product.id == item.product_id))).scalar_one_or_none()
        if prod is not None and prod.cost_basis_usd is not None:
            return (prod.cost_basis_usd * item.quantity).quantize(_Q_MONEY)
    purity = KARAT_PURITY[item.karat]
    return (item.gold_rate_at_sale * item.weight_grams * purity * item.quantity).quantize(_Q_MONEY)


async def post_sale(db: AsyncSession, order, settings: Settings, actor_user_id: str):
    if not auto_post_enabled(settings):
        return None
    if await find_live_entry(db, SOURCE_ORDER, order.id):
        return None
    entry_date = order.created_at.date() if order.created_at else date.today()
    await ensure_period(db, entry_date)

    cash_key = "BANK" if order.payment_method == PaymentMethod.CARD else "CASH"
    making_revenue = sum(
        (it.making_charge * it.quantity for it in order.items), ZERO
    ).quantize(_Q_MONEY)
    sales_revenue = (order.subtotal - making_revenue - order.discount_amount).quantize(_Q_MONEY)

    cash_id = await resolve_account_id(db, cash_key)
    rev_id = await resolve_account_id(db, "SALES_REVENUE")
    making_id = await resolve_account_id(db, "MAKING_CHARGE_REVENUE")
    vat_id = await resolve_account_id(db, "VAT_PAYABLE")
    cogs_id = await resolve_account_id(db, "METAL_COGS")
    inv_id = await resolve_account_id(db, "METAL_INVENTORY")

    lines = [
        gl.GLLine(account_id=cash_id, denomination="MONEY",
                  base_debit=order.total_usd, money_debit=order.total_usd, memo="sale cash"),
        gl.GLLine(account_id=rev_id, denomination="MONEY",
                  base_credit=sales_revenue, money_credit=sales_revenue, memo="sale revenue"),
    ]
    if making_revenue > 0:
        lines.append(gl.GLLine(account_id=making_id, denomination="MONEY",
                               base_credit=making_revenue, money_credit=making_revenue, memo="making charge"))
    if order.vat_amount > 0:
        lines.append(gl.GLLine(account_id=vat_id, denomination="MONEY",
                               base_credit=order.vat_amount, money_credit=order.vat_amount, memo="output VAT"))

    # COGS aggregated per karat
    cogs: dict[str, dict] = {}
    for it in order.items:
        k = it.karat.value
        grams = (it.weight_grams * it.quantity)
        cost = await _cogs_cost_for_item(db, it)
        agg = cogs.setdefault(k, {"grams": ZERO, "cost": ZERO})
        agg["grams"] += grams
        agg["cost"] += cost
    for k, v in cogs.items():
        lines.append(gl.GLLine(account_id=cogs_id, denomination="DUAL",
                               base_debit=v["cost"], metal_debit_grams=v["grams"], karat=k, memo="metal COGS"))
        lines.append(gl.GLLine(account_id=inv_id, denomination="DUAL",
                               base_credit=v["cost"], metal_credit_grams=v["grams"], karat=k, memo="metal out"))

    return await gl.post_entry(
        db, entry_date=entry_date, memo=f"Sale {order.order_number}",
        source_type=SOURCE_ORDER, source_id=order.id, lines=lines, actor_user_id=actor_user_id,
    )


class _RefundItemView:
    """Adapts an OrderItem to the shape _cogs_cost_for_item expects, with
    quantity = the refunded qty (so cost is the refunded portion)."""
    def __init__(self, item):
        self.item_kind = item.item_kind
        self.product_id = item.product_id
        self.karat = item.karat
        self.weight_grams = item.weight_grams
        self.gold_rate_at_sale = item.gold_rate_at_sale
        self.quantity = item.refunded_qty or 1


async def post_order_refund(db: AsyncSession, order, settings: Settings, actor_user_id: str,
                            *, refunded_item=None, refund_seq: int = 0):
    """Full void/refund → reverse the original ORDER entry. Per-item refund →
    a targeted reversing entry for just that line's revenue + VAT share + COGS."""
    if not auto_post_enabled(settings):
        return None
    original = await find_live_entry(db, SOURCE_ORDER, order.id)
    if original is None:
        return None  # nothing was posted (e.g. flag was off at sale time)

    if refunded_item is None:
        if await find_live_entry(db, SOURCE_ORDER_REFUND, order.id):
            return None
        return await gl.reverse_entry(
            db, original_entry_id=original.id, actor_user_id=actor_user_id,
            entry_date=date.today(), memo=f"Void {order.order_number}",
        )

    # Partial per-item refund: reverse only this line's portion.
    src_id = f"{refunded_item.id}:{refund_seq}"
    if await find_live_entry(db, SOURCE_ORDER_REFUND, src_id):
        return None
    line_value = refunded_item.refunded_amount  # pre-VAT value just refunded
    vat_share = (line_value * settings.vat_percent / Decimal(100)).quantize(_Q_MONEY)
    making_share = (refunded_item.making_charge * (refunded_item.refunded_qty or 1)).quantize(_Q_MONEY)
    sales_share = (line_value - making_share).quantize(_Q_MONEY)
    cash_back = (line_value + vat_share).quantize(_Q_MONEY)
    grams = refunded_item.weight_grams * (refunded_item.refunded_qty or 1)
    cost = await _cogs_cost_for_item(db, _RefundItemView(refunded_item))

    cash_id = await resolve_account_id(db, "CASH")
    rev_id = await resolve_account_id(db, "SALES_REVENUE")
    making_id = await resolve_account_id(db, "MAKING_CHARGE_REVENUE")
    vat_id = await resolve_account_id(db, "VAT_PAYABLE")
    cogs_id = await resolve_account_id(db, "METAL_COGS")
    inv_id = await resolve_account_id(db, "METAL_INVENTORY")

    lines = [
        gl.GLLine(account_id=rev_id, denomination="MONEY", base_debit=sales_share, money_debit=sales_share, memo="refund revenue"),
        gl.GLLine(account_id=vat_id, denomination="MONEY", base_debit=vat_share, money_debit=vat_share, memo="refund VAT"),
        gl.GLLine(account_id=cash_id, denomination="MONEY", base_credit=cash_back, money_credit=cash_back, memo="refund cash out"),
        gl.GLLine(account_id=inv_id, denomination="DUAL", base_debit=cost, metal_debit_grams=grams, karat=refunded_item.karat.value, memo="metal back"),
        gl.GLLine(account_id=cogs_id, denomination="DUAL", base_credit=cost, metal_credit_grams=grams, karat=refunded_item.karat.value, memo="reverse COGS"),
    ]
    if making_share > 0:
        lines.insert(2, gl.GLLine(account_id=making_id, denomination="MONEY",
                                  base_debit=making_share, money_debit=making_share, memo="refund making"))
    return await gl.post_entry(
        db, entry_date=date.today(), memo=f"Refund {order.order_number} line",
        source_type=SOURCE_ORDER_REFUND, source_id=src_id, lines=lines, actor_user_id=actor_user_id,
    )


async def post_supplier_purchase(db: AsyncSession, purchase, settings: Settings, actor_user_id: str):
    if not auto_post_enabled(settings):
        return None
    if await find_live_entry(db, SOURCE_SUPPLIER_PURCHASE, purchase.id):
        return None
    entry_date = purchase.occurred_at.date() if purchase.occurred_at else date.today()
    await ensure_period(db, entry_date)

    inv_id = await resolve_account_id(db, "METAL_INVENTORY")
    metal_ap_id = await resolve_account_id(db, "METAL_AP")
    prod_inv_id = await resolve_account_id(db, "PRODUCT_INVENTORY")
    ap_id = await resolve_account_id(db, "AP")

    lines: list[gl.GLLine] = []

    # Gold rows: per karat grams + cost from gold items.
    gold_cost_by_karat: dict[str, Decimal] = {}
    gold_grams_by_karat: dict[str, Decimal] = {}
    for it in purchase.items:
        if it.item_kind == SupplierItemKind.PURE_GOLD and it.karat is not None:
            k = it.karat.value
            gold_grams_by_karat[k] = gold_grams_by_karat.get(k, ZERO) + (it.weight_grams or ZERO)
            gold_cost_by_karat[k] = gold_cost_by_karat.get(k, ZERO) + (it.unit_cost_usd or ZERO)
    for k, grams in gold_grams_by_karat.items():
        cost = gold_cost_by_karat.get(k, ZERO).quantize(_Q_MONEY)
        lines.append(gl.GLLine(account_id=inv_id, denomination="DUAL",
                               base_debit=cost, metal_debit_grams=grams, karat=k, memo="gold purchase"))
        lines.append(gl.GLLine(account_id=metal_ap_id, denomination="DUAL",
                               base_credit=cost, metal_credit_grams=grams, karat=k, memo="metal AP"))

    # Non-gold (product/coin/ounce) rows: money cost → Product Inventory vs AP.
    cash_cost = sum(
        ((it.unit_cost_usd or ZERO) for it in purchase.items
         if it.item_kind != SupplierItemKind.PURE_GOLD), ZERO
    ).quantize(_Q_MONEY)
    if cash_cost > 0:
        lines.append(gl.GLLine(account_id=prod_inv_id, denomination="MONEY",
                               base_debit=cash_cost, money_debit=cash_cost, memo="product purchase"))
        lines.append(gl.GLLine(account_id=ap_id, denomination="MONEY",
                               base_credit=cash_cost, money_credit=cash_cost, memo="AP"))

    if not lines:
        return None
    return await gl.post_entry(
        db, entry_date=entry_date, memo="Supplier purchase",
        source_type=SOURCE_SUPPLIER_PURCHASE, source_id=purchase.id, lines=lines, actor_user_id=actor_user_id,
    )


async def post_supplier_payment(db: AsyncSession, payment, settings: Settings, actor_user_id: str):
    if not auto_post_enabled(settings):
        return None
    if await find_live_entry(db, SOURCE_SUPPLIER_PAYMENT, payment.id):
        return None
    entry_date = payment.paid_at.date() if payment.paid_at else date.today()
    await ensure_period(db, entry_date)

    if payment.unit == DebtUnit.CASH:
        ap_id = await resolve_account_id(db, "AP")
        cash_id = await resolve_account_id(db, "CASH")
        amt = payment.amount.quantize(_Q_MONEY)
        lines = [
            gl.GLLine(account_id=ap_id, denomination="MONEY", base_debit=amt, money_debit=amt, memo="pay AP"),
            gl.GLLine(account_id=cash_id, denomination="MONEY", base_credit=amt, money_credit=amt, memo="cash out"),
        ]
    else:  # GOLD: pay down metal AP grams with inventory grams (cost-neutral metal move)
        metal_ap_id = await resolve_account_id(db, "METAL_AP")
        inv_id = await resolve_account_id(db, "METAL_INVENTORY")
        k = payment.karat.value
        grams = payment.amount
        lines = [
            gl.GLLine(account_id=metal_ap_id, denomination="DUAL", metal_debit_grams=grams, karat=k, memo="pay metal AP"),
            gl.GLLine(account_id=inv_id, denomination="DUAL", metal_credit_grams=grams, karat=k, memo="metal out"),
        ]
    return await gl.post_entry(
        db, entry_date=entry_date, memo="Supplier payment",
        source_type=SOURCE_SUPPLIER_PAYMENT, source_id=payment.id, lines=lines, actor_user_id=actor_user_id,
    )


async def post_buyback(db: AsyncSession, buyback, settings: Settings, actor_user_id: str):
    """Gold acquired for cash: DR Metal Inventory (cost+grams), CR Cash (cost),
    CR Metal Clearing (grams) — the metal counterpart for metal-crosses-money."""
    if not auto_post_enabled(settings):
        return None
    if await find_live_entry(db, SOURCE_BUYBACK, buyback.id):
        return None
    entry_date = buyback.occurred_at.date() if buyback.occurred_at else date.today()
    await ensure_period(db, entry_date)
    if buyback.karat is None or not buyback.weight_grams:
        return None  # nothing metal to post (defensive)

    inv_id = await resolve_account_id(db, "METAL_INVENTORY")
    cash_id = await resolve_account_id(db, "CASH")
    clearing_id = await resolve_account_id(db, "METAL_CLEARING")
    k = buyback.karat.value
    grams = buyback.weight_grams * (buyback.quantity or 1)
    cost = buyback.buy_price_usd.quantize(_Q_MONEY)
    lines = [
        gl.GLLine(account_id=inv_id, denomination="DUAL", base_debit=cost,
                  metal_debit_grams=grams, karat=k, memo="buyback in"),
        gl.GLLine(account_id=cash_id, denomination="MONEY", base_credit=cost,
                  money_credit=cost, memo="buyback cash out"),
        gl.GLLine(account_id=clearing_id, denomination="DUAL",
                  metal_credit_grams=grams, karat=k, memo="metal acquired (clearing)"),
    ]
    return await gl.post_entry(
        db, entry_date=entry_date, memo="Walk-in buyback",
        source_type=SOURCE_BUYBACK, source_id=buyback.id, lines=lines, actor_user_id=actor_user_id,
    )


async def post_melt(db: AsyncSession, melt, settings: Settings, actor_user_id: str):
    """Karat conversion: cost preserved within Metal Inventory; each karat's
    grams balance through Metal Clearing."""
    if not auto_post_enabled(settings):
        return None
    if await find_live_entry(db, SOURCE_MELT, melt.id):
        return None
    entry_date = melt.occurred_at.date() if melt.occurred_at else date.today()
    await ensure_period(db, entry_date)
    inv_id = await resolve_account_id(db, "METAL_INVENTORY")
    clearing_id = await resolve_account_id(db, "METAL_CLEARING")
    cost = melt.cost_usd.quantize(_Q_MONEY)
    fk, fg = melt.from_karat.value, melt.from_grams
    tk, tg = melt.to_karat.value, melt.to_grams
    lines = [
        gl.GLLine(account_id=inv_id, denomination="DUAL", base_credit=cost,
                  metal_credit_grams=fg, karat=fk, memo="melt consume"),
        gl.GLLine(account_id=clearing_id, denomination="DUAL", metal_debit_grams=fg, karat=fk, memo="melt clearing A"),
        gl.GLLine(account_id=inv_id, denomination="DUAL", base_debit=cost,
                  metal_debit_grams=tg, karat=tk, memo="melt result"),
        gl.GLLine(account_id=clearing_id, denomination="DUAL", metal_credit_grams=tg, karat=tk, memo="melt clearing B"),
    ]
    return await gl.post_entry(
        db, entry_date=entry_date, memo="Melt / refining",
        source_type=SOURCE_MELT, source_id=melt.id, lines=lines, actor_user_id=actor_user_id,
    )


async def post_adjustment(db: AsyncSession, adj, settings: Settings, actor_user_id: str):
    """Metal inventory loss/theft/gift: DR Adjustment Expense (cost), CR Metal
    Inventory (cost+grams), DR Metal Clearing (grams)."""
    if not auto_post_enabled(settings):
        return None
    if await find_live_entry(db, SOURCE_ADJUSTMENT, adj.id):
        return None
    entry_date = adj.occurred_at.date() if adj.occurred_at else date.today()
    await ensure_period(db, entry_date)
    if adj.karat is None or not adj.grams:
        return None
    exp_id = await resolve_account_id(db, "ADJUSTMENT_EXPENSE")
    inv_id = await resolve_account_id(db, "METAL_INVENTORY")
    clearing_id = await resolve_account_id(db, "METAL_CLEARING")
    k = adj.karat.value
    cost = adj.cost_usd.quantize(_Q_MONEY)
    lines = [
        gl.GLLine(account_id=exp_id, denomination="MONEY", base_debit=cost, money_debit=cost, memo="inventory loss"),
        gl.GLLine(account_id=inv_id, denomination="DUAL", base_credit=cost,
                  metal_credit_grams=adj.grams, karat=k, memo="metal written off"),
        gl.GLLine(account_id=clearing_id, denomination="DUAL", metal_debit_grams=adj.grams, karat=k, memo="metal loss (clearing)"),
    ]
    return await gl.post_entry(
        db, entry_date=entry_date, memo="Inventory adjustment",
        source_type=SOURCE_ADJUSTMENT, source_id=adj.id, lines=lines, actor_user_id=actor_user_id,
    )
