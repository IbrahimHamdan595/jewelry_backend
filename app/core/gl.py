"""GL Core posting engine + pure validators (Module 0).

Mirrors app/core/ledger.py: pure helpers carry the logic, the DB wrapper
runs inside the caller's transaction with NO commit. The GL chain head is
locked FOR UPDATE during posting to serialize appends, exactly like
InventoryLedger.record().

Balancing model (design §3.2): every posted entry must satisfy BOTH
  • money dimension: Σ base_debit == Σ base_credit (USD base), and
  • metal dimension PER KARAT: Σ grams_debit(k) == Σ grams_credit(k).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ledger
from app.core.audit_chain import compute_gl_entry_hash
from app.models import (
    Denomination, GLAccount, GLEntrySequence, GLJournalChainHead,
    GLJournalEntry, GLJournalLine, GLPeriod, PeriodStatus,
)

ZERO = Decimal("0")

# Source-type constants (strings, like InventoryLedger event types).
SOURCE_MANUAL = "MANUAL"
SOURCE_OPENING = "OPENING"
SOURCE_REVERSAL = "REVERSAL"
# Future operation sources (M1): ORDER, SUPPLIER_PURCHASE, SUPPLIER_PAYMENT, ...


@dataclass
class GLLine:
    """One proposed journal line. `denomination` is authoritative from the
    account in the DB path (post_entry resets it); callers of the pure
    validator pass it directly."""
    account_id: str
    denomination: str  # "MONEY" | "METAL" | "DUAL"
    base_debit: Decimal = ZERO
    base_credit: Decimal = ZERO
    money_debit: Decimal = ZERO
    money_credit: Decimal = ZERO
    currency: str = "USD"
    fx_rate: Decimal = Decimal("1")
    metal_debit_grams: Decimal = ZERO
    metal_credit_grams: Decimal = ZERO
    karat: str | None = None
    memo: str = ""


def _has_money(ln: GLLine) -> bool:
    return bool(ln.base_debit or ln.base_credit or ln.money_debit or ln.money_credit)


def _has_metal(ln: GLLine) -> bool:
    return bool(ln.metal_debit_grams or ln.metal_credit_grams)


def validate_balanced(lines: list[GLLine]) -> list[str]:
    """Return a list of human-readable errors; empty list means valid.

    Pure — no DB, no clock. Checks (design §3.2):
      1. ≥1 line.
      2. Money dimension nets to zero in USD base.
      3. Metal dimension nets to zero per karat.
      4. Each component matches its account's denomination.
      5. Any metal component carries a karat.
    """
    if not lines:
        return ["at least one line is required"]

    errors: list[str] = []

    money_debit = sum((ln.base_debit for ln in lines), ZERO)
    money_credit = sum((ln.base_credit for ln in lines), ZERO)
    if money_debit != money_credit:
        errors.append(
            f"money dimension unbalanced: base debits {money_debit} != base credits {money_credit}"
        )

    karats = {ln.karat for ln in lines if _has_metal(ln)}
    for k in sorted(str(x) for x in karats):
        kd = sum((ln.metal_debit_grams for ln in lines if ln.karat == k), ZERO)
        kc = sum((ln.metal_credit_grams for ln in lines if ln.karat == k), ZERO)
        if kd != kc:
            errors.append(f"metal dimension unbalanced for {k}: debit grams {kd} != credit grams {kc}")

    for ln in lines:
        if ln.denomination == Denomination.MONEY.value and _has_metal(ln):
            errors.append(f"MONEY account {ln.account_id} cannot carry a metal component")
        if ln.denomination == Denomination.METAL.value and _has_money(ln):
            errors.append(f"METAL account {ln.account_id} cannot carry a money component")
        if _has_metal(ln) and not ln.karat:
            errors.append(f"metal line on {ln.account_id} requires a karat")

    return errors


async def _resolve_open_period(db: AsyncSession, entry_date: date) -> GLPeriod:
    """Find the monthly period for `entry_date` and require it OPEN."""
    period = (
        await db.execute(
            select(GLPeriod).where(
                GLPeriod.year == entry_date.year,
                GLPeriod.period_no == entry_date.month,
            )
        )
    ).scalar_one_or_none()
    if period is None:
        raise HTTPException(
            status_code=422,
            detail=f"No accounting period for {entry_date.year}-{entry_date.month:02d}. "
                   f"Open it first.",
        )
    if period.status != PeriodStatus.OPEN:
        raise HTTPException(
            status_code=422,
            detail=f"Accounting period {entry_date.year}-{entry_date.month:02d} is CLOSED.",
        )
    return period


async def _next_entry_no(db: AsyncSession, entry_date: date) -> str:
    """Allocate JE-YYYYMMDD-NNN via a per-day counter row locked FOR UPDATE.

    During posting this runs while the chain-head lock is already held, so the
    initial INSERT of a new day_key never races; the FOR UPDATE here also makes
    direct callers safe."""
    day_key = entry_date.strftime("%Y%m%d")
    row = (
        await db.execute(
            select(GLEntrySequence).where(GLEntrySequence.day_key == day_key).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        row = GLEntrySequence(day_key=day_key, last_seq=0)
        db.add(row)
        await db.flush()
    row.last_seq = row.last_seq + 1
    await db.flush()
    return f"JE-{day_key}-{row.last_seq:03d}"


async def _resolve_denominations(db: AsyncSession, lines: list[GLLine]) -> None:
    """Overwrite each line's `denomination` with the account's DB value, and
    require the account to exist and be active. Authority lives in the DB, not
    the caller (prevents a caller smuggling a metal component onto a MONEY
    account)."""
    ids = {ln.account_id for ln in lines}
    rows = (await db.execute(select(GLAccount).where(GLAccount.id.in_(ids)))).scalars().all()
    by_id = {a.id: a for a in rows}
    for ln in lines:
        acct = by_id.get(ln.account_id)
        if acct is None:
            raise HTTPException(status_code=422, detail=f"Unknown GL account {ln.account_id}")
        if not acct.is_active:
            raise HTTPException(status_code=422, detail=f"GL account {acct.code} is inactive")
        ln.denomination = acct.denomination.value


def _line_to_hash_dict(ln: GLLine) -> dict:
    return {
        "account_id": ln.account_id,
        "money_debit": ln.money_debit, "money_credit": ln.money_credit,
        "currency": ln.currency, "fx_rate": ln.fx_rate,
        "base_debit": ln.base_debit, "base_credit": ln.base_credit,
        "metal_debit_grams": ln.metal_debit_grams, "metal_credit_grams": ln.metal_credit_grams,
        "karat": ln.karat, "memo": ln.memo,
    }


async def post_entry(
    db: AsyncSession,
    *,
    entry_date: date,
    memo: str,
    source_type: str,
    source_id: str | None,
    lines: list[GLLine],
    actor_user_id: str,
    reverses_entry_id: str | None = None,
    occurred_at: datetime | None = None,
) -> GLJournalEntry:
    """Post a balanced journal entry inside the caller's transaction (no commit).

    Order (design §3.4): resolve OPEN period → resolve denominations from DB →
    validate balance → lock chain head → allocate entry_no → compute hash →
    insert header+lines → advance head → record GL_ENTRY_POSTED audit event.

    Lock ordering note: the GL chain head is locked BEFORE InventoryLedger's
    head (inside ledger.record). Always acquire GL-head-then-inventory-head to
    avoid deadlocks.
    """
    period = await _resolve_open_period(db, entry_date)
    await _resolve_denominations(db, lines)

    errors = validate_balanced(lines)
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))

    head = (
        await db.execute(
            select(GLJournalChainHead).where(GLJournalChainHead.id == 1).with_for_update()
        )
    ).scalar_one()

    entry_no = await _next_entry_no(db, entry_date)
    occurred = occurred_at or datetime.now(timezone.utc)

    header = {
        "entry_no": entry_no, "entry_date": entry_date, "memo": memo,
        "source_type": source_type, "source_id": source_id,
        "reverses_entry_id": reverses_entry_id, "actor_user_id": actor_user_id,
        "occurred_at": occurred,
    }
    line_dicts = [_line_to_hash_dict(ln) for ln in lines]
    entry_hash = compute_gl_entry_hash(prev_hash=head.latest_entry_hash, header=header, lines=line_dicts)

    entry = GLJournalEntry(
        entry_no=entry_no, entry_date=entry_date, period_id=period.id, memo=memo,
        source_type=source_type, source_id=source_id, reverses_entry_id=reverses_entry_id,
        actor_user_id=actor_user_id, occurred_at=occurred,
        prev_hash=head.latest_entry_hash, entry_hash=entry_hash,
    )
    db.add(entry)
    await db.flush()

    for i, ln in enumerate(lines):
        db.add(GLJournalLine(
            entry_id=entry.id, line_no=i, account_id=ln.account_id,
            money_debit=ln.money_debit, money_credit=ln.money_credit,
            currency=ln.currency, fx_rate=ln.fx_rate,
            base_debit=ln.base_debit, base_credit=ln.base_credit,
            metal_debit_grams=ln.metal_debit_grams, metal_credit_grams=ln.metal_credit_grams,
            karat=ln.karat, memo=ln.memo,
        ))

    head.latest_entry_hash = entry_hash
    head.row_count = head.row_count + 1

    await ledger.record(
        db,
        event_type=ledger.EVENT_GL_ENTRY_POSTED,
        actor_user_id=actor_user_id,
        ref_type="gl_journal_entry",
        ref_id=entry.id,
        payload={
            "entry_no": entry_no, "source_type": source_type, "source_id": source_id,
            "line_count": len(lines),
            "base_debit_total": str(sum((ln.base_debit for ln in lines), ZERO)),
        },
    )
    await db.flush()
    return entry
