"""Module 7b — financial KPIs derived from the M7a statements + GL snapshots.

Read-only. Each ratio guards divide-by-zero by returning value None. Averages
use opening (trial balance at start−1) and closing (at end) snapshots.
"""
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.gl import _q_money, compute_trial_balance
from app.core.statements import (
    COGS_KEYS,  # noqa: F401  (kept for symmetry / future use)
    INVENTORY_KEYS,
    MONEY_AP_KEYS,
    income_statement,
)
from app.models import AccountType, GLAccount, GLJournalEntry, GLJournalLine

ZERO = Decimal("0")
_Q2 = Decimal("0.01")
_Q3 = Decimal("0.001")


def _q2(v):
    return None if v is None else v.quantize(_Q2)


def _div(numer: Decimal, denom: Decimal):
    """Decimal division returning None when the denominator is zero."""
    if denom is None or denom == ZERO:
        return None
    return numer / denom


async def _snapshot(db: AsyncSession, *, as_of: date) -> dict:
    """Balance snapshot at a point in time, grouped for KPI inputs."""
    tb = await compute_trial_balance(db, as_of=as_of)
    inv = ar = ap = metal_grams = cur_assets = cur_liab = ZERO
    for a in tb["accounts"]:
        t, key = a["type"], a["system_key"]
        debit, credit = a["base_debit"], a["base_credit"]
        if t == AccountType.ASSET.value:
            net = debit - credit
            cur_assets += net
            if key in INVENTORY_KEYS:
                inv += net
                for mv in a["metal_by_karat"].values():
                    metal_grams += mv["net_grams"]
            if key == "AR":
                ar += net
        elif t == AccountType.LIABILITY.value:
            net = credit - debit
            cur_liab += net
            if key in MONEY_AP_KEYS:
                ap += net
    return {"inventory": inv, "ar": ar, "ap": ap, "metal_grams": metal_grams,
            "current_assets": cur_assets, "current_liabilities": cur_liab}


async def _flows(db: AsyncSession, *, start: date, end: date) -> dict:
    """In-window flows: credit sales (AR debits) and metal COGS grams."""
    rows = (await db.execute(
        select(GLJournalLine, GLAccount)
        .join(GLJournalEntry, GLJournalLine.entry_id == GLJournalEntry.id)
        .join(GLAccount, GLJournalLine.account_id == GLAccount.id)
        .where(GLJournalEntry.entry_date >= start, GLJournalEntry.entry_date <= end)
    )).all()
    credit_sales = metal_cogs_grams = ZERO
    for line, account in rows:
        if account.system_key == "AR":
            credit_sales += line.base_debit
        if account.system_key == "METAL_COGS":
            metal_cogs_grams += (line.metal_debit_grams - line.metal_credit_grams)
    return {"credit_sales": credit_sales, "metal_cogs_grams": metal_cogs_grams}


async def compute_kpis(db: AsyncSession, *, start: date, end: date) -> dict:
    days = (end - start).days + 1
    opening = await _snapshot(db, as_of=start - timedelta(days=1))
    closing = await _snapshot(db, as_of=end)
    flows = await _flows(db, start=start, end=end)
    pnl = await income_statement(db, start=start, end=end)

    cogs = pnl["cogs"]
    revenue = pnl["revenue"]
    avg_inv = (opening["inventory"] + closing["inventory"]) / 2
    avg_ap = (opening["ap"] + closing["ap"]) / 2
    avg_ar = (opening["ar"] + closing["ar"]) / 2
    avg_metal = (opening["metal_grams"] + closing["metal_grams"]) / 2
    d = Decimal(days)

    dsi = _div(avg_inv, cogs)
    dsi = dsi * d if dsi is not None else None
    turnover = _div(cogs, avg_inv)
    dpo = _div(avg_ap, cogs)
    dpo = dpo * d if dpo is not None else None
    gross_margin = _div(pnl["gross_profit"], revenue)
    gross_margin = gross_margin * 100 if gross_margin is not None else None
    net_margin = _div(pnl["net_profit"], revenue)
    net_margin = net_margin * 100 if net_margin is not None else None
    metal_turnover = _div(flows["metal_cogs_grams"], avg_metal)
    dso = _div(avg_ar, flows["credit_sales"])
    dso = dso * d if dso is not None else None
    ccc = (dso + dsi - dpo) if (dso is not None and dsi is not None and dpo is not None) else None
    current_ratio = _div(closing["current_assets"], closing["current_liabilities"])
    quick_ratio = _div(closing["current_assets"] - closing["inventory"], closing["current_liabilities"])

    return {
        "start": start, "end": end, "days": days,
        "dsi": {"value": _q2(dsi), "avg_inventory": _q_money(avg_inv), "cogs": cogs},
        "inventory_turnover": {"value": _q2(turnover), "avg_inventory": _q_money(avg_inv), "cogs": cogs},
        "dpo": {"value": _q2(dpo), "avg_ap": _q_money(avg_ap), "cogs": cogs},
        "gross_margin": {"value": _q2(gross_margin), "gross_profit": pnl["gross_profit"], "revenue": revenue},
        "net_margin": {"value": _q2(net_margin), "net_profit": pnl["net_profit"], "revenue": revenue},
        "metal_turnover": {"value": _q2(metal_turnover), "avg_metal_grams": avg_metal.quantize(_Q3),
                           "metal_cogs_grams": flows["metal_cogs_grams"].quantize(_Q3)},
        "dso": {"value": _q2(dso), "avg_ar": _q_money(avg_ar), "credit_sales": _q_money(flows["credit_sales"])},
        "ccc": {"value": _q2(ccc), "dso": _q2(dso), "dsi": _q2(dsi), "dpo": _q2(dpo)},
        "current_ratio": {"value": _q2(current_ratio), "current_assets": _q_money(closing["current_assets"]),
                          "current_liabilities": _q_money(closing["current_liabilities"])},
        "quick_ratio": {"value": _q2(quick_ratio), "current_assets": _q_money(closing["current_assets"]),
                        "inventory": _q_money(closing["inventory"]),
                        "current_liabilities": _q_money(closing["current_liabilities"])},
    }
