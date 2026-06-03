"""Module 7 — financial statements as read-only GL replays.

Every function here is a pure replay over immutable journal lines, mirroring
`gl.compute_trial_balance`'s discipline (design §3.5): no cached state, as-of
correctness for free. No function mutates the DB.
"""
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.gl import _q_grams, _q_money, compute_trial_balance
from app.models import (
    AccountType,
    BankAccount,
    GLAccount,
    GLJournalEntry,
    GLJournalLine,
)

ZERO = Decimal("0")

COGS_KEYS = {"METAL_COGS", "MAKING_COGS"}
INVENTORY_KEYS = {"METAL_INVENTORY", "PRODUCT_INVENTORY"}
MONEY_AP_KEYS = {"AP", "VENDOR_AP"}


def _line(a: dict, amount: Decimal) -> dict:
    return {"code": a["code"], "name": a["name"], "system_key": a["system_key"], "amount": amount}


async def income_statement(db: AsyncSession, *, start: date, end: date) -> dict:
    """Revenue / COGS / operating-expense breakdown over [start, end].

    Income accounts carry a natural credit balance (amount = credit − debit);
    expense accounts a natural debit balance (amount = debit − credit). COGS is
    the subset of expense accounts whose system_key is in COGS_KEYS.
    """
    rows = (await db.execute(
        select(GLJournalLine, GLAccount)
        .join(GLJournalEntry, GLJournalLine.entry_id == GLJournalEntry.id)
        .join(GLAccount, GLJournalLine.account_id == GLAccount.id)
        .where(GLJournalEntry.entry_date >= start, GLJournalEntry.entry_date <= end)
        .where(GLJournalEntry.source_type != "YEAR_CLOSE")
        .where(GLAccount.type.in_((AccountType.INCOME, AccountType.EXPENSE)))
    )).all()

    acc: dict[str, dict] = {}
    for line, account in rows:
        a = acc.setdefault(account.id, {
            "code": account.code, "name": account.name,
            "system_key": account.system_key, "type": account.type,
            "debit": ZERO, "credit": ZERO,
        })
        a["debit"] += line.base_debit
        a["credit"] += line.base_credit

    revenue_lines, cogs_lines, opex_lines = [], [], []
    revenue = cogs = opex = ZERO
    for a in sorted(acc.values(), key=lambda x: x["code"]):
        if a["type"] == AccountType.INCOME:
            amt = _q_money(a["credit"] - a["debit"])
            revenue += amt
            revenue_lines.append(_line(a, amt))
        elif a["system_key"] in COGS_KEYS:
            amt = _q_money(a["debit"] - a["credit"])
            cogs += amt
            cogs_lines.append(_line(a, amt))
        else:
            amt = _q_money(a["debit"] - a["credit"])
            opex += amt
            opex_lines.append(_line(a, amt))

    revenue, cogs, opex = _q_money(revenue), _q_money(cogs), _q_money(opex)
    gross = _q_money(revenue - cogs)
    return {
        "start": start, "end": end,
        "revenue_lines": revenue_lines, "cogs_lines": cogs_lines, "opex_lines": opex_lines,
        "revenue": revenue, "cogs": cogs, "gross_profit": gross,
        "operating_expenses": opex, "net_profit": _q_money(gross - opex),
    }


def _bs_line(a: dict, amount: Decimal) -> dict:
    return {"code": a["code"], "name": a["name"], "system_key": a["system_key"], "amount": amount}


async def balance_sheet(db: AsyncSession, *, as_of: date) -> dict:
    """Assets vs Liabilities + Equity as of `as_of`.

    Because income/expense accounts are not closed until period-close (M8), a
    synthetic 'Current-period earnings' equity line = Σ net income to-date makes
    Assets == Liabilities + Equity hold exactly (the TB always balances). All
    assets/liabilities are treated as current until Fixed Assets (T2a) —
    flagged via all_current.
    """
    tb = await compute_trial_balance(db, as_of=as_of)

    asset_lines, liability_lines, equity_lines = [], [], []
    total_assets = total_liabilities = total_equity = ZERO
    net_income = ZERO
    metal_position: dict[str, Decimal] = {}

    for a in tb["accounts"]:  # already sorted by code
        t = a["type"]
        debit, credit = a["base_debit"], a["base_credit"]
        if t == AccountType.ASSET.value:
            amt = _q_money(debit - credit)
            total_assets += amt
            asset_lines.append(_bs_line(a, amt))
            for k, mv in a["metal_by_karat"].items():
                metal_position[k] = metal_position.get(k, ZERO) + mv["net_grams"]
        elif t == AccountType.LIABILITY.value:
            amt = _q_money(credit - debit)
            total_liabilities += amt
            liability_lines.append(_bs_line(a, amt))
        elif t == AccountType.EQUITY.value:
            amt = _q_money(credit - debit)
            total_equity += amt
            equity_lines.append(_bs_line(a, amt))
        elif t == AccountType.INCOME.value:
            net_income += (credit - debit)
        elif t == AccountType.EXPENSE.value:
            net_income += (credit - debit)

    net_income = _q_money(net_income)
    total_equity = _q_money(total_equity + net_income)
    equity_lines.append({"code": "—", "name": "Current-period earnings",
                         "system_key": "CURRENT_EARNINGS", "amount": net_income})

    total_assets = _q_money(total_assets)
    total_liabilities = _q_money(total_liabilities)
    return {
        "as_of": as_of, "all_current": True,
        "asset_lines": asset_lines, "liability_lines": liability_lines,
        "equity_lines": equity_lines,
        "total_assets": total_assets, "total_liabilities": total_liabilities,
        "total_equity": total_equity,
        "balanced": total_assets == _q_money(total_liabilities + total_equity),
        "metal_position": [
            {"karat": k, "net_grams": _q_grams(v)} for k, v in sorted(metal_position.items()) if v != ZERO
        ],
    }


_CF_CATEGORIES = [
    ("sales", "Sales & receipts"),
    ("purchases", "Supplier & purchase payments"),
    ("expenses", "Operating expenses paid"),
    ("tax", "Tax"),
    ("owner", "Owner / equity"),
    ("transfers", "Transfers"),
    ("other", "Other"),
]


async def _cash_account_ids(db: AsyncSession) -> set[str]:
    return set((await db.execute(select(BankAccount.gl_account_id))).scalars().all())


async def _cash_balance(db: AsyncSession, *, as_of: date, cash_ids: set[str]) -> Decimal:
    tb = await compute_trial_balance(db, as_of=as_of)
    total = ZERO
    for a in tb["accounts"]:
        if a["account_id"] in cash_ids:
            total += (a["base_debit"] - a["base_credit"])
    return _q_money(total)


def _counterpart_category(account: GLAccount, cash_ids: set[str]) -> str:
    if account.id in cash_ids:
        return "transfers"
    key = account.system_key
    t = account.type
    if t == AccountType.INCOME or key == "AR":
        return "sales"
    if key in ("AP", "VENDOR_AP", "METAL_AP") or key in INVENTORY_KEYS:
        return "purchases"
    if key in ("VAT_PAYABLE", "VAT_RECEIVABLE"):
        return "tax"
    if t == AccountType.EXPENSE:
        return "expenses"
    if t == AccountType.EQUITY:
        return "owner"
    return "other"


async def cash_flow(db: AsyncSession, *, start: date, end: date) -> dict:
    """Direct cash-movement statement: opening cash + categorized in-window cash
    deltas == closing cash. Each in-window line on a cash account is bucketed by
    the dominant counterpart (largest non-cash sibling line in its entry).
    Asserts reconciliation (guard, like the TB identity)."""
    cash_ids = await _cash_account_ids(db)
    opening = await _cash_balance(db, as_of=start - timedelta(days=1), cash_ids=cash_ids)
    closing = await _cash_balance(db, as_of=end, cash_ids=cash_ids)

    # All in-window lines, grouped by entry, so we can find each cash line's counterpart.
    rows = (await db.execute(
        select(GLJournalLine, GLAccount)
        .join(GLJournalEntry, GLJournalLine.entry_id == GLJournalEntry.id)
        .join(GLAccount, GLJournalLine.account_id == GLAccount.id)
        .where(GLJournalEntry.entry_date >= start, GLJournalEntry.entry_date <= end)
    )).all()

    by_entry: dict[str, list] = {}
    for line, account in rows:
        by_entry.setdefault(line.entry_id, []).append((line, account))

    totals = {key: ZERO for key, _ in _CF_CATEGORIES}
    for entry_lines in by_entry.values():
        cash_lines = [(l, a) for (l, a) in entry_lines if a.id in cash_ids]
        if not cash_lines:
            continue
        # Dominant non-cash counterpart by absolute base amount.
        non_cash = [(l, a) for (l, a) in entry_lines if a.id not in cash_ids]
        if non_cash:
            cp_line, cp_acct = max(
                non_cash, key=lambda la: abs(la[0].base_debit - la[0].base_credit))
            category = _counterpart_category(cp_acct, cash_ids)
        else:
            category = "transfers"
        for line, _ in cash_lines:
            totals[category] += (line.money_debit - line.money_credit)

    categories = [
        {"key": key, "label": label, "amount": _q_money(totals[key])}
        for key, label in _CF_CATEGORIES if totals[key] != ZERO
    ]
    net_change = _q_money(sum(totals.values(), ZERO))
    return {
        "start": start, "end": end,
        "opening_cash": opening, "closing_cash": closing,
        "categories": categories, "net_change": net_change,
        "reconciles": _q_money(opening + net_change) == closing,
    }


async def account_statement(db: AsyncSession, *, account_id: str, start: date, end: date) -> dict:
    """Ledger for one account: opening balance (debit−credit before `start`),
    each in-window line with a running balance, and closing balance. Dual
    accounts also carry running grams."""
    account = (await db.execute(
        select(GLAccount).where(GLAccount.id == account_id))).scalar_one()

    # Opening: net (debit − credit) of all lines before the window.
    opening_rows = (await db.execute(
        select(GLJournalLine)
        .join(GLJournalEntry, GLJournalLine.entry_id == GLJournalEntry.id)
        .where(GLJournalLine.account_id == account_id,
               GLJournalEntry.entry_date < start)
    )).scalars().all()
    opening = sum((l.base_debit - l.base_credit for l in opening_rows), ZERO)
    opening_grams = sum((l.metal_debit_grams - l.metal_credit_grams for l in opening_rows), ZERO)

    window_rows = (await db.execute(
        select(GLJournalLine, GLJournalEntry)
        .join(GLJournalEntry, GLJournalLine.entry_id == GLJournalEntry.id)
        .where(GLJournalLine.account_id == account_id,
               GLJournalEntry.entry_date >= start,
               GLJournalEntry.entry_date <= end)
        .order_by(GLJournalEntry.entry_date, GLJournalEntry.entry_no)
    )).all()

    running = opening
    running_grams = opening_grams
    rows = []
    for line, entry in window_rows:
        running += (line.base_debit - line.base_credit)
        running_grams += (line.metal_debit_grams - line.metal_credit_grams)
        rows.append({
            "entry_id": entry.id, "entry_no": entry.entry_no,
            "date": entry.entry_date, "memo": entry.memo,
            "debit": _q_money(line.base_debit), "credit": _q_money(line.base_credit),
            "running_balance": _q_money(running),
            "metal_debit_grams": _q_grams(line.metal_debit_grams),
            "metal_credit_grams": _q_grams(line.metal_credit_grams),
            "running_grams": _q_grams(running_grams),
        })

    return {
        "account_id": account.id, "code": account.code, "name": account.name,
        "type": account.type.value, "system_key": account.system_key,
        "start": start, "end": end,
        "opening_balance": _q_money(opening), "closing_balance": _q_money(running),
        "opening_grams": _q_grams(opening_grams), "closing_grams": _q_grams(running_grams),
        "rows": rows,
    }
