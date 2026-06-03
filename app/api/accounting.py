from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import gl, ledger, period_close
from app.core.audit_chain import verify_gl_chain
from app.core.coa_seed import seed_chart_of_accounts, post_opening_balances
from app.core.permissions import require_accounting, require_admin
from app.deps import get_db
from app.models import (
    AccountType, Denomination, GLAccount, GLJournalChainHead, GLJournalEntry,
    GLJournalLine, GLPeriod, NormalBalance, PeriodStatus, User,
)
from app.schemas.accounting import (
    AccountCreate, AccountOut, AccountUpdate, JournalEntryCreate, JournalEntryOut,
    OpeningBalancesCreate, PeriodOpen, PeriodOut,
)

router = APIRouter(prefix="/accounting", tags=["accounting"])


async def _load_entry(db: AsyncSession, entry_id: str) -> GLJournalEntry:
    """Re-fetch an entry with its lines eager-loaded so serialization never
    lazy-loads (which fails in async context after commit)."""
    return (
        await db.execute(
            select(GLJournalEntry)
            .options(selectinload(GLJournalEntry.lines))
            .where(GLJournalEntry.id == entry_id)
        )
    ).scalar_one()


# ── Chart of accounts ─────────────────────────────────────────────────────────

@router.get("/accounts")
async def list_accounts(db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    rows = (await db.execute(select(GLAccount).order_by(GLAccount.code))).scalars().all()
    return {"items": [AccountOut.model_validate(r) for r in rows]}


@router.post("/accounts", response_model=AccountOut)
async def create_account(
    body: AccountCreate, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_accounting),
):
    acct = GLAccount(
        code=body.code, name=body.name, type=AccountType(body.type),
        denomination=Denomination(body.denomination),
        normal_balance=NormalBalance(body.normal_balance),
        parent_id=body.parent_id, currency=body.currency,
    )
    db.add(acct)
    await db.flush()
    await ledger.record(db, event_type=ledger.EVENT_GL_ACCOUNT_CREATED,
                        actor_user_id=user.id, ref_type="gl_account", ref_id=acct.id,
                        payload={"code": acct.code, "name": acct.name})
    await db.commit()
    return AccountOut.model_validate(acct)


@router.patch("/accounts/{account_id}", response_model=AccountOut)
async def update_account(
    account_id: str, body: AccountUpdate, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_accounting),
):
    acct = (await db.execute(select(GLAccount).where(GLAccount.id == account_id))).scalar_one_or_none()
    if acct is None:
        raise HTTPException(404, "Account not found")
    # system_key accounts can be deactivated but only by ADMIN; renamed by anyone.
    if body.is_active is False and acct.system_key:
        if user.role.value != "ADMIN":
            raise HTTPException(403, "Only ADMIN may deactivate a system account")
    if body.name is not None:
        acct.name = body.name
    if body.parent_id is not None:
        acct.parent_id = body.parent_id
    if body.is_active is not None:
        acct.is_active = body.is_active
    await ledger.record(db, event_type=ledger.EVENT_GL_ACCOUNT_UPDATED,
                        actor_user_id=user.id, ref_type="gl_account", ref_id=acct.id,
                        payload={"code": acct.code})
    await db.commit()
    return AccountOut.model_validate(acct)


@router.post("/seed-coa")
async def seed_coa(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    created = await seed_chart_of_accounts(db)
    await db.commit()
    return {"created": created}


# ── Periods ───────────────────────────────────────────────────────────────────

@router.get("/periods")
async def list_periods(db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    rows = (await db.execute(select(GLPeriod).order_by(GLPeriod.year, GLPeriod.period_no))).scalars().all()
    return {"items": [PeriodOut.model_validate(r) for r in rows]}


@router.post("/periods", response_model=PeriodOut)
async def open_period(
    body: PeriodOpen, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin),
):
    existing = (
        await db.execute(select(GLPeriod).where(GLPeriod.year == body.year, GLPeriod.period_no == body.period_no))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "Period already exists")
    p = GLPeriod(year=body.year, period_no=body.period_no, status=PeriodStatus.OPEN)
    db.add(p)
    await db.commit()
    return PeriodOut.model_validate(p)


@router.post("/periods/{period_id}/close", response_model=PeriodOut)
async def close_period(
    period_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin),
):
    p = (await db.execute(select(GLPeriod).where(GLPeriod.id == period_id))).scalar_one_or_none()
    if p is None:
        raise HTTPException(404, "Period not found")
    readiness = await period_close.close_readiness(db, year=p.year, period_no=p.period_no)
    if not readiness["can_close"]:
        failed = [h for h in readiness["hard"] if not h["ok"]]
        raise HTTPException(422, "Cannot close: " + "; ".join(h["detail"] for h in failed))
    p.status = PeriodStatus.CLOSED
    p.closed_by_user_id = user.id
    p.closed_at = datetime.now(timezone.utc)
    await ledger.record(db, event_type=ledger.EVENT_GL_PERIOD_CLOSED, actor_user_id=user.id,
                        ref_type="gl_period", ref_id=p.id, payload={"year": p.year, "period_no": p.period_no})
    await db.commit()
    return PeriodOut.model_validate(p)


@router.post("/periods/{period_id}/reopen", response_model=PeriodOut)
async def reopen_period(
    period_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin),
):
    p = (await db.execute(select(GLPeriod).where(GLPeriod.id == period_id))).scalar_one_or_none()
    if p is None:
        raise HTTPException(404, "Period not found")
    p.status = PeriodStatus.OPEN
    p.closed_at = None
    await ledger.record(db, event_type=ledger.EVENT_GL_PERIOD_REOPENED, actor_user_id=user.id,
                        ref_type="gl_period", ref_id=p.id, payload={"year": p.year, "period_no": p.period_no})
    await db.commit()
    return PeriodOut.model_validate(p)


# ── Period close & controls (Module 8) ────────────────────────────────────────

def _S_close(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, dict):
        return {k: _S_close(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_S_close(x) for x in v]
    return v


@router.get("/periods/close-readiness")
async def close_readiness_endpoint(
    year: int, period_no: int, db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting),
):
    return _S_close(await period_close.close_readiness(db, year=year, period_no=period_no))


@router.get("/periods/year-close-preview")
async def year_close_preview_endpoint(
    year: int, db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting),
):
    return _S_close(await period_close.year_close_preview(db, year=year))


@router.post("/periods/close-year")
async def close_year_endpoint(
    body: dict, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin),
):
    year = int(body["year"])
    entry = await period_close.close_year(db, year=year, actor_user_id=user.id)
    await db.commit()
    opened = (await db.execute(
        select(GLPeriod).where(GLPeriod.year == year + 1).order_by(GLPeriod.period_no))).scalars().all()
    return {"entry_id": entry.id, "entry_no": entry.entry_no,
            "opened_periods": [f"{p.year}-{p.period_no:02d}" for p in opened]}


# ── Journal entries ───────────────────────────────────────────────────────────

@router.get("/journal-entries")
async def list_entries(
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting),
):
    base = (
        select(GLJournalEntry)
        .options(selectinload(GLJournalEntry.lines))
        .order_by(GLJournalEntry.occurred_at.desc())
    )
    total = (await db.execute(select(func.count()).select_from(GLJournalEntry))).scalar_one()
    rows = (await db.execute(base.offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"items": [JournalEntryOut.model_validate(r) for r in rows], "total": total,
            "page": page, "page_size": page_size}


@router.get("/journal-entries/{entry_id}", response_model=JournalEntryOut)
async def get_entry(entry_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    e = (
        await db.execute(
            select(GLJournalEntry)
            .options(selectinload(GLJournalEntry.lines))
            .where(GLJournalEntry.id == entry_id)
        )
    ).scalar_one_or_none()
    if e is None:
        raise HTTPException(404, "Entry not found")
    return JournalEntryOut.model_validate(e)


@router.post("/journal-entries", response_model=JournalEntryOut)
async def create_entry(
    body: JournalEntryCreate, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_accounting),
):
    lines = [
        gl.GLLine(
            account_id=ln.account_id, denomination="",  # resolved from DB in post_entry
            money_debit=ln.money_debit, money_credit=ln.money_credit,
            currency=ln.currency, fx_rate=ln.fx_rate,
            base_debit=ln.base_debit, base_credit=ln.base_credit,
            metal_debit_grams=ln.metal_debit_grams, metal_credit_grams=ln.metal_credit_grams,
            karat=ln.karat, memo=ln.memo,
        )
        for ln in body.lines
    ]
    entry = await gl.post_entry(
        db, entry_date=body.entry_date, memo=body.memo, source_type=body.source_type,
        source_id=body.source_id, lines=lines, actor_user_id=user.id,
    )
    await db.commit()
    return JournalEntryOut.model_validate(await _load_entry(db, entry.id))


@router.post("/journal-entries/{entry_id}/reverse", response_model=JournalEntryOut)
async def reverse(
    entry_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_accounting),
):
    rev = await gl.reverse_entry(db, original_entry_id=entry_id, actor_user_id=user.id,
                                 entry_date=date.today(), memo="")
    await db.commit()
    return JournalEntryOut.model_validate(await _load_entry(db, rev.id))


# ── Trial balance + opening balances + verify ─────────────────────────────────

def _stringify(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, dict):
        return {k: _stringify(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_stringify(x) for x in v]
    return v


@router.get("/trial-balance")
async def trial_balance(
    as_of: date, db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting),
):
    tb = await gl.compute_trial_balance(db, as_of=as_of)
    return _stringify(tb)


@router.post("/opening-balances", response_model=JournalEntryOut)
async def opening_balances(
    body: OpeningBalancesCreate, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    entry = await post_opening_balances(
        db, as_of=body.as_of, actor_user_id=user.id,
        cash_lines=[{"system_key": c.system_key, "amount": c.amount} for c in body.cash_lines],
    )
    await db.commit()
    return JournalEntryOut.model_validate(await _load_entry(db, entry.id))


@router.get("/ledger/verify")
async def verify(db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    entries = (
        await db.execute(select(GLJournalEntry).order_by(GLJournalEntry.occurred_at, GLJournalEntry.id))
    ).scalars().all()
    rows = []
    for e in entries:
        lines = (
            await db.execute(select(GLJournalLine).where(GLJournalLine.entry_id == e.id).order_by(GLJournalLine.line_no))
        ).scalars().all()
        rows.append({
            "id": e.id, "prev_hash": e.prev_hash, "entry_hash": e.entry_hash,
            "entry_no": e.entry_no, "entry_date": e.entry_date, "memo": e.memo,
            "source_type": e.source_type, "source_id": e.source_id,
            "reverses_entry_id": e.reverses_entry_id, "actor_user_id": e.actor_user_id,
            "occurred_at": e.occurred_at,
            "lines": [
                {
                    "account_id": l.account_id, "money_debit": l.money_debit, "money_credit": l.money_credit,
                    "currency": l.currency, "fx_rate": l.fx_rate, "base_debit": l.base_debit,
                    "base_credit": l.base_credit, "metal_debit_grams": l.metal_debit_grams,
                    "metal_credit_grams": l.metal_credit_grams, "karat": l.karat, "memo": l.memo,
                }
                for l in lines
            ],
        })
    result = verify_gl_chain(rows)
    head = (await db.execute(select(GLJournalChainHead).where(GLJournalChainHead.id == 1))).scalar_one()
    computed = entries[-1].entry_hash if entries else "GENESIS"
    return {**result, "head_row_count": head.row_count, "head_latest_hash": head.latest_entry_hash,
            "computed_latest_hash": computed,
            "head_matches": head.latest_entry_hash == computed and head.row_count == len(entries)}
