"""Module 8 — period close controls + year-end closing entry.

Read-mostly: the only write is the year-close journal entry (an ordinary
immutable, hash-chained entry tagged source_type='YEAR_CLOSE'). No new tables.
"""
from calendar import monthrange
from datetime import date
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import gl, ledger
from app.core.audit_chain import verify_gl_chain
from app.core.gl import _q_money, compute_trial_balance
from app.models import (
    AccountType,
    BankStatementLine,
    GLAccount,
    GLJournalEntry,
    GLJournalLine,
    GLPeriod,
    PeriodStatus,
    VendorBill,
    VendorBillStatus,
)

ZERO = Decimal("0")
YEAR_CLOSE = "YEAR_CLOSE"


def period_month_range(year: int, period_no: int) -> tuple[date, date]:
    last = monthrange(year, period_no)[1]
    return date(year, period_no, 1), date(year, period_no, last)


async def _gl_chain_intact(db: AsyncSession) -> bool:
    entries = (await db.execute(
        select(GLJournalEntry).order_by(GLJournalEntry.occurred_at, GLJournalEntry.id))).scalars().all()
    rows = []
    for e in entries:
        lines = (await db.execute(
            select(GLJournalLine).where(GLJournalLine.entry_id == e.id)
            .order_by(GLJournalLine.line_no))).scalars().all()
        rows.append({
            "id": e.id, "prev_hash": e.prev_hash, "entry_hash": e.entry_hash,
            "entry_no": e.entry_no, "entry_date": e.entry_date, "memo": e.memo,
            "source_type": e.source_type, "source_id": e.source_id,
            "reverses_entry_id": e.reverses_entry_id, "actor_user_id": e.actor_user_id,
            "occurred_at": e.occurred_at,
            "lines": [{
                "account_id": l.account_id, "money_debit": l.money_debit, "money_credit": l.money_credit,
                "currency": l.currency, "fx_rate": l.fx_rate, "base_debit": l.base_debit,
                "base_credit": l.base_credit, "metal_debit_grams": l.metal_debit_grams,
                "metal_credit_grams": l.metal_credit_grams, "karat": l.karat, "memo": l.memo,
            } for l in lines],
        })
    return verify_gl_chain(rows)["status"] in ("intact", "empty")


async def close_readiness(db: AsyncSession, *, year: int, period_no: int) -> dict:
    # Hard: no earlier period still OPEN.
    open_earlier = (await db.execute(
        select(GLPeriod).where(GLPeriod.status == PeriodStatus.OPEN).where(
            (GLPeriod.year < year) |
            ((GLPeriod.year == year) & (GLPeriod.period_no < period_no))
        ))).scalars().all()
    prior_ok = len(open_earlier) == 0
    prior_detail = ("all earlier periods closed" if prior_ok
                    else "earlier period(s) still open: " +
                    ", ".join(f"{p.year}-{p.period_no:02d}" for p in open_earlier))

    chain_ok = await _gl_chain_intact(db)

    # Soft: unreconciled bank lines + open vendor bills in the month.
    start, end = period_month_range(year, period_no)
    unrec = (await db.execute(
        select(BankStatementLine).where(
            BankStatementLine.matched_gl_line_id.is_(None),
            BankStatementLine.stmt_date >= start, BankStatementLine.stmt_date <= end,
        ))).scalars().all()
    open_bills = (await db.execute(
        select(VendorBill).where(
            VendorBill.status.in_((VendorBillStatus.OPEN, VendorBillStatus.PARTIAL)),
            VendorBill.bill_date >= start, VendorBill.bill_date <= end,
        ))).scalars().all()

    hard = [
        {"key": "prior_periods_closed", "ok": prior_ok, "detail": prior_detail},
        {"key": "chain_intact", "ok": chain_ok,
         "detail": "ledger hash chain intact" if chain_ok else "ledger hash chain BROKEN"},
    ]
    soft = [
        {"key": "unreconciled_bank_lines", "count": len(unrec),
         "detail": f"{len(unrec)} unreconciled bank statement line(s) this month"},
        {"key": "open_vendor_bills", "count": len(open_bills),
         "detail": f"{len(open_bills)} open vendor bill(s) dated this month"},
    ]
    return {"year": year, "period_no": period_no,
            "can_close": all(h["ok"] for h in hard), "hard": hard, "soft": soft}


async def _year_already_closed(db: AsyncSession, year: int) -> bool:
    start, end = date(year, 1, 1), date(year, 12, 31)
    existing = (await db.execute(
        select(GLJournalEntry).where(
            GLJournalEntry.source_type == YEAR_CLOSE,
            GLJournalEntry.entry_date >= start, GLJournalEntry.entry_date <= end,
        ))).scalars().first()
    return existing is not None


async def year_close_preview(db: AsyncSession, *, year: int) -> dict:
    """The money-only closing entry that close_year would post for `year`."""
    tb = await compute_trial_balance(db, as_of=date(year, 12, 31))
    lines = []
    income_total = expense_total = ZERO
    for a in tb["accounts"]:
        debit, credit = a["base_debit"], a["base_credit"]
        if a["type"] == AccountType.INCOME.value:
            bal = credit - debit  # natural credit balance
            if bal != ZERO:
                income_total += bal
                lines.append({"account_id": a["account_id"], "code": a["code"], "name": a["name"],
                              "system_key": a["system_key"], "debit": _q_money(bal), "credit": ZERO})
        elif a["type"] == AccountType.EXPENSE.value:
            bal = debit - credit  # natural debit balance
            if bal != ZERO:
                expense_total += bal
                lines.append({"account_id": a["account_id"], "code": a["code"], "name": a["name"],
                              "system_key": a["system_key"], "debit": ZERO, "credit": _q_money(bal)})

    net_income = _q_money(income_total - expense_total)
    re = (await db.execute(
        select(GLAccount).where(GLAccount.system_key == "RETAINED_EARNINGS"))).scalar_one()
    if net_income > ZERO:
        lines.append({"account_id": re.id, "code": re.code, "name": re.name,
                      "system_key": "RETAINED_EARNINGS", "debit": ZERO, "credit": net_income})
    elif net_income < ZERO:
        lines.append({"account_id": re.id, "code": re.code, "name": re.name,
                      "system_key": "RETAINED_EARNINGS", "debit": -net_income, "credit": ZERO})

    return {"year": year, "lines": lines, "net_income": net_income,
            "retained_earnings_delta": net_income,
            "already_closed": await _year_already_closed(db, year)}


async def close_year(db: AsyncSession, *, year: int, actor_user_id: str) -> GLJournalEntry:
    # Guard: no OPEN period in the year.
    open_periods = (await db.execute(
        select(GLPeriod).where(GLPeriod.year == year, GLPeriod.status == PeriodStatus.OPEN))).scalars().all()
    if open_periods:
        raise HTTPException(422, "Cannot close year — open period(s): " +
                            ", ".join(f"{p.year}-{p.period_no:02d}" for p in open_periods))
    any_period = (await db.execute(
        select(GLPeriod).where(GLPeriod.year == year))).scalars().first()
    if any_period is None:
        raise HTTPException(422, f"No periods exist for {year}.")
    if await _year_already_closed(db, year):
        raise HTTPException(409, f"Fiscal year {year} is already closed.")

    preview = await year_close_preview(db, year=year)
    if not preview["lines"]:
        raise HTTPException(422, f"Nothing to close for {year} (no P&L balances).")

    # Ensure the December period exists (closing entry is dated YYYY-12-31).
    dec = (await db.execute(
        select(GLPeriod).where(GLPeriod.year == year, GLPeriod.period_no == 12))).scalar_one_or_none()
    if dec is None:
        db.add(GLPeriod(year=year, period_no=12, status=PeriodStatus.CLOSED))
        await db.flush()

    # Convert preview lines to money-only GLLines.
    lines = []
    for l in preview["lines"]:
        lines.append(gl.GLLine(
            account_id=l["account_id"], denomination="MONEY",
            base_debit=l["debit"], base_credit=l["credit"],
            money_debit=l["debit"], money_credit=l["credit"], currency="USD",
            memo="year close"))

    entry = await gl.post_entry(
        db, entry_date=date(year, 12, 31), memo=f"Year close {year}",
        source_type=YEAR_CLOSE, source_id=None, lines=lines,
        actor_user_id=actor_user_id, allow_closed_period=True)

    await ledger.record(
        db, event_type=ledger.EVENT_GL_YEAR_CLOSED, actor_user_id=actor_user_id,
        ref_type="fiscal_year", ref_id=str(year),
        payload={"year": year, "net_income": str(preview["net_income"]), "entry_id": entry.id})

    # Auto-open next year's 12 periods (skip existing).
    existing_next = {p.period_no for p in (await db.execute(
        select(GLPeriod).where(GLPeriod.year == year + 1))).scalars().all()}
    for m in range(1, 13):
        if m not in existing_next:
            db.add(GLPeriod(year=year + 1, period_no=m, status=PeriodStatus.OPEN))
    await db.flush()
    return entry
