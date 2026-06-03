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
    GLAccount, GLJournalEntry, GLPeriod, OrderItemKind, PaymentMethod,
    PeriodStatus, Product, Settings,
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
