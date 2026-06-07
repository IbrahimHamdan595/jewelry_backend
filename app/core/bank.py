"""Cash & Bank (Module 2): bank accounts (each backed by a MONEY gl_account),
transfers, statement import, reconciliation. Clearing state lives on the
statement line because gl_journal_line is immutable."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import gl, ledger
from app.models import (
    AccountType, BankAccount, BankAccountType, BankStatementLine, Denomination,
    GLAccount, GLJournalEntry, GLJournalLine, NormalBalance, Reconciliation,
    ReconciliationStatus, StatementLineStatus,
)

ZERO = Decimal("0")
_Q_MONEY = Decimal("0.01")

SOURCE_TRANSFER = "TRANSFER"

_SEEDED_MAP = {
    "CASH": BankAccountType.CASH, "CASH_LBP": BankAccountType.CASH,
    "BANK": BankAccountType.BANK, "CASH_PETTY": BankAccountType.PETTY_CASH,
    # Card clearing is wrapped as a BANK-type account so it appears in transfer
    # dropdowns; settlement is a transfer clearing → real bank (design v2 §7.2).
    "CREDIT_CARD_CLEARING": BankAccountType.BANK,
}


# Standard Lebanese liquidity bands (6-digit posting codes) per account type.
_CODE_BANDS = {
    BankAccountType.BANK: (512200, 512999),
    BankAccountType.CASH: (530000, 530999),
    BankAccountType.PETTY_CASH: (530000, 530999),
}


async def next_bank_account_code(db: AsyncSession, account_type: BankAccountType) -> str:
    """First free numeric code in the standard liquidity band for this type."""
    lo, hi = _CODE_BANDS[account_type]
    rows = (await db.execute(select(GLAccount.code))).scalars().all()
    used = {int(c) for c in rows if c.isdigit() and lo <= int(c) <= hi}
    n = lo + 1
    while n in used:
        n += 1
    if n > hi:
        raise HTTPException(status_code=422, detail=f"No free {account_type.value} code in {lo}-{hi}")
    return str(n)


async def create_bank_account(db: AsyncSession, *, name: str, account_type: BankAccountType,
                              currency: str, bank_name: str | None, account_number: str | None,
                              actor_user_id: str) -> BankAccount:
    code = await next_bank_account_code(db, account_type)
    acct = GLAccount(code=code, name=name, type=AccountType.ASSET,
                     denomination=Denomination.MONEY, normal_balance=NormalBalance.DEBIT,
                     currency=currency, system_key=None)
    db.add(acct)
    await db.flush()
    ba = BankAccount(name=name, gl_account_id=acct.id, account_type=account_type,
                     currency=currency, bank_name=bank_name, account_number=account_number)
    db.add(ba)
    await db.flush()
    await ledger.record(db, event_type=ledger.EVENT_GL_ACCOUNT_CREATED, actor_user_id=actor_user_id,
                        ref_type="bank_account", ref_id=ba.id,
                        payload={"name": name, "gl_code": code, "type": account_type.value})
    return ba


async def adopt_seeded_accounts(db: AsyncSession) -> int:
    """Ensure a bank_account exists for each seeded CASH/CASH_LBP/BANK gl_account."""
    existing = {b.gl_account_id for b in (await db.execute(select(BankAccount))).scalars().all()}
    created = 0
    for key, btype in _SEEDED_MAP.items():
        acct = (await db.execute(select(GLAccount).where(GLAccount.system_key == key))).scalar_one_or_none()
        if acct is None or acct.id in existing:
            continue
        db.add(BankAccount(name=acct.name, gl_account_id=acct.id, account_type=btype,
                           currency=acct.currency or "USD"))
        created += 1
    await db.flush()
    return created


def usd_base(amount: Decimal, currency: str, lbp_rate: Decimal) -> Decimal:
    if currency == "LBP":
        return (amount / lbp_rate).quantize(_Q_MONEY)
    return amount.quantize(_Q_MONEY)


async def post_transfer(db: AsyncSession, *, from_account: BankAccount, to_account: BankAccount,
                        amount: Decimal, dest_amount: Decimal | None, memo: str,
                        entry_date: date, actor_user_id: str, lbp_rate: Decimal):
    if from_account.id == to_account.id:
        raise HTTPException(status_code=422, detail="Cannot transfer to the same account")
    if amount <= 0:
        raise HTTPException(status_code=422, detail="amount must be positive")
    dest_amt = dest_amount if dest_amount is not None else amount

    src_base = usd_base(amount, from_account.currency, lbp_rate)
    dest_base = usd_base(dest_amt, to_account.currency, lbp_rate)

    lines = [
        gl.GLLine(account_id=to_account.gl_account_id, denomination="MONEY",
                  base_debit=dest_base, money_debit=dest_amt.quantize(_Q_MONEY),
                  currency=to_account.currency, memo=f"transfer in: {memo}"),
        gl.GLLine(account_id=from_account.gl_account_id, denomination="MONEY",
                  base_credit=src_base, money_credit=amount.quantize(_Q_MONEY),
                  currency=from_account.currency, memo=f"transfer out: {memo}"),
    ]
    residual = (src_base - dest_base).quantize(_Q_MONEY)
    if residual != ZERO:
        # Split FX (Odoo parity): a debit residual is a loss, a credit a gain.
        fx_key = "FX_LOSS" if residual > 0 else "FX_GAIN"
        fx = (await db.execute(select(GLAccount).where(GLAccount.system_key == fx_key))).scalar_one()
        if residual > 0:
            lines.append(gl.GLLine(account_id=fx.id, denomination="MONEY",
                                   base_debit=residual, money_debit=residual, memo="transfer FX"))
        else:
            lines.append(gl.GLLine(account_id=fx.id, denomination="MONEY",
                                   base_credit=-residual, money_credit=-residual, memo="transfer FX"))

    return await gl.post_entry(db, entry_date=entry_date, memo=f"Transfer: {memo}",
                               source_type=SOURCE_TRANSFER, source_id=uuid4().hex,
                               lines=lines, actor_user_id=actor_user_id)


async def import_statement(db: AsyncSession, *, bank_account_id: str, rows: list[dict]) -> int:
    for r in rows:
        db.add(BankStatementLine(
            bank_account_id=bank_account_id, stmt_date=r["stmt_date"],
            description=r.get("description", ""), amount=Decimal(str(r["amount"])).quantize(_Q_MONEY),
            reference=r.get("reference"),
        ))
    await db.flush()
    return len(rows)


async def _gl_lines_for_account(db: AsyncSession, gl_account_id: str):
    return (
        await db.execute(
            select(GLJournalLine, GLJournalEntry)
            .join(GLJournalEntry, GLJournalLine.entry_id == GLJournalEntry.id)
            .where(GLJournalLine.account_id == gl_account_id)
        )
    ).all()


async def suggest_matches(db: AsyncSession, *, bank_account_id: str, window_days: int = 5) -> list[dict]:
    ba = (await db.execute(select(BankAccount).where(BankAccount.id == bank_account_id))).scalar_one()
    cleared = {
        sid for (sid,) in (
            await db.execute(
                select(BankStatementLine.matched_gl_line_id)
                .where(BankStatementLine.status == StatementLineStatus.MATCHED,
                       BankStatementLine.matched_gl_line_id.isnot(None))
            )
        ).all()
    }
    gl_rows = await _gl_lines_for_account(db, ba.gl_account_id)
    unmatched = (
        await db.execute(
            select(BankStatementLine).where(
                BankStatementLine.bank_account_id == bank_account_id,
                BankStatementLine.status == StatementLineStatus.UNMATCHED,
            )
        )
    ).scalars().all()

    out: list[dict] = []
    used: set[str] = set()
    for sl in unmatched:
        want_debit = sl.amount > 0
        target = abs(sl.amount)
        for line, entry in gl_rows:
            if line.id in cleared or line.id in used:
                continue
            amt = line.money_debit if want_debit else line.money_credit
            if amt != target:
                continue
            if abs((entry.entry_date - sl.stmt_date).days) > window_days:
                continue
            out.append({"statement_line_id": sl.id, "gl_line_id": line.id,
                        "gl_date": entry.entry_date, "amount": str(sl.amount)})
            used.add(line.id)
            break
    return out


async def apply_match(db: AsyncSession, *, statement_line_id: str, gl_line_id: str) -> None:
    sl = (await db.execute(select(BankStatementLine).where(BankStatementLine.id == statement_line_id))).scalar_one()
    if sl.status == StatementLineStatus.MATCHED:
        if sl.matched_gl_line_id == gl_line_id:
            return  # idempotent
        raise HTTPException(status_code=422, detail="Statement line already matched to a different GL line")
    sl.matched_gl_line_id = gl_line_id
    sl.status = StatementLineStatus.MATCHED
    await db.flush()


async def unmatch(db: AsyncSession, statement_line_id: str) -> None:
    sl = (await db.execute(select(BankStatementLine).where(BankStatementLine.id == statement_line_id))).scalar_one()
    sl.matched_gl_line_id = None
    sl.status = StatementLineStatus.UNMATCHED
    await db.flush()


async def start_reconciliation(db: AsyncSession, *, bank_account_id: str, statement_date: date,
                               statement_balance: Decimal, actor_user_id: str) -> Reconciliation:
    rec = Reconciliation(bank_account_id=bank_account_id, statement_date=statement_date,
                         statement_balance=Decimal(str(statement_balance)).quantize(_Q_MONEY),
                         started_by_user_id=actor_user_id)
    db.add(rec)
    await db.flush()
    lines = (await db.execute(
        select(BankStatementLine).where(
            BankStatementLine.bank_account_id == bank_account_id,
            BankStatementLine.stmt_date <= statement_date,
        )
    )).scalars().all()
    for sl in lines:
        sl.reconciliation_id = rec.id
    await db.flush()
    return rec


async def compute_reconciliation(db: AsyncSession, reconciliation_id: str) -> dict:
    rec = (await db.execute(select(Reconciliation).where(Reconciliation.id == reconciliation_id))).scalar_one()
    ba = (await db.execute(select(BankAccount).where(BankAccount.id == rec.bank_account_id))).scalar_one()
    gl_rows = await _gl_lines_for_account(db, ba.gl_account_id)
    gl_balance = sum(
        ((line.money_debit - line.money_credit) for line, entry in gl_rows
         if entry.entry_date <= rec.statement_date), ZERO
    ).quantize(_Q_MONEY)
    matched = (await db.execute(
        select(BankStatementLine).where(
            BankStatementLine.reconciliation_id == reconciliation_id,
            BankStatementLine.status == StatementLineStatus.MATCHED,
        )
    )).scalars().all()
    unmatched = (await db.execute(
        select(BankStatementLine).where(
            BankStatementLine.reconciliation_id == reconciliation_id,
            BankStatementLine.status == StatementLineStatus.UNMATCHED,
        )
    )).scalars().all()
    cleared_amount = sum((sl.amount for sl in matched), ZERO).quantize(_Q_MONEY)
    difference = (rec.statement_balance - gl_balance).quantize(_Q_MONEY)
    rec.gl_balance = gl_balance
    rec.cleared_amount = cleared_amount
    rec.difference = difference
    await db.flush()
    return {
        "reconciliation_id": rec.id, "statement_balance": rec.statement_balance,
        "gl_balance": gl_balance, "cleared_amount": cleared_amount, "difference": difference,
        "matched_count": len(matched), "unmatched_count": len(unmatched),
    }


async def complete_reconciliation(db: AsyncSession, reconciliation_id: str, actor_user_id: str) -> Reconciliation:
    rec = (await db.execute(select(Reconciliation).where(Reconciliation.id == reconciliation_id))).scalar_one()
    await compute_reconciliation(db, reconciliation_id)
    rec.status = ReconciliationStatus.COMPLETED
    rec.completed_at = datetime.now(timezone.utc)
    ba = (await db.execute(select(BankAccount).where(BankAccount.id == rec.bank_account_id))).scalar_one()
    ba.last_reconciled_at = datetime.now(timezone.utc)
    await db.flush()
    return rec
