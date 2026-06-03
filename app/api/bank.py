import csv
import io
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import bank
from app.core.permissions import require_accounting
from app.deps import get_db
from app.models import (
    BankAccount, BankAccountType, BankStatementLine, GLJournalLine, Reconciliation, Settings, User,
)
from app.schemas.bank import (
    BankAccountCreate, BankAccountOut, MatchRequest, ReconciliationStart, TransferCreate,
)

router = APIRouter(prefix="/accounting/bank", tags=["accounting-bank"])


def _stringify(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, dict):
        return {k: _stringify(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_stringify(x) for x in v]
    return v


async def _lbp_rate(db: AsyncSession) -> Decimal:
    s = (await db.execute(select(Settings).where(Settings.id == "singleton"))).scalar_one_or_none()
    return s.lbp_exchange_rate if s else Decimal("89500")


async def _account_balance(db: AsyncSession, gl_account_id: str) -> dict:
    rows = (await db.execute(select(GLJournalLine).where(GLJournalLine.account_id == gl_account_id))).scalars().all()
    money = sum((l.money_debit - l.money_credit for l in rows), Decimal("0"))
    base = sum((l.base_debit - l.base_credit for l in rows), Decimal("0"))
    return {"money": str(money.quantize(Decimal("0.01"))), "base": str(base.quantize(Decimal("0.01")))}


@router.post("/adopt-seeded")
async def adopt_seeded(db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    created = await bank.adopt_seeded_accounts(db)
    await db.commit()
    return {"created": created}


@router.get("/accounts")
async def list_accounts(db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    rows = (await db.execute(select(BankAccount).order_by(BankAccount.name))).scalars().all()
    return {"items": [BankAccountOut.model_validate(r) for r in rows]}


@router.post("/accounts", response_model=BankAccountOut)
async def create_account(body: BankAccountCreate, db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_accounting)):
    ba = await bank.create_bank_account(db, name=body.name, account_type=BankAccountType(body.account_type),
                                        currency=body.currency, bank_name=body.bank_name,
                                        account_number=body.account_number, actor_user_id=user.id)
    await db.commit()
    return BankAccountOut.model_validate(ba)


@router.get("/cash-position")
async def cash_position(db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    rows = (await db.execute(select(BankAccount).where(BankAccount.is_active.is_(True)))).scalars().all()
    out = []
    for ba in rows:
        bal = await _account_balance(db, ba.gl_account_id)
        out.append({"id": ba.id, "name": ba.name, "account_type": ba.account_type.value,
                    "currency": ba.currency, "balance_money": bal["money"], "balance_base": bal["base"],
                    "last_reconciled_at": ba.last_reconciled_at})
    return {"accounts": out}


@router.post("/transfers")
async def transfer(body: TransferCreate, db: AsyncSession = Depends(get_db),
                   user: User = Depends(require_accounting)):
    src = (await db.execute(select(BankAccount).where(BankAccount.id == body.from_account_id))).scalar_one_or_none()
    dst = (await db.execute(select(BankAccount).where(BankAccount.id == body.to_account_id))).scalar_one_or_none()
    if not src or not dst:
        raise HTTPException(404, "Account not found")
    entry = await bank.post_transfer(db, from_account=src, to_account=dst, amount=body.amount,
                                     dest_amount=body.dest_amount, memo=body.memo, entry_date=body.entry_date,
                                     actor_user_id=user.id, lbp_rate=await _lbp_rate(db))
    await db.commit()
    return {"entry_no": entry.entry_no, "id": entry.id}


@router.post("/accounts/{account_id}/statement-import")
async def statement_import(account_id: str, file: UploadFile = File(...),
                           db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    raw = (await file.read()).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw))
    norm = {h.lower().strip(): h for h in (reader.fieldnames or [])}
    if "date" not in norm or "amount" not in norm:
        raise HTTPException(422, "CSV must have 'date' and 'amount' columns")
    rows = []
    for i, row in enumerate(reader, start=2):
        try:
            rows.append({
                "stmt_date": date.fromisoformat(row[norm["date"]].strip()),
                "description": row.get(norm.get("description", ""), "").strip() if norm.get("description") else "",
                "amount": Decimal(row[norm["amount"]].strip()),
                "reference": row.get(norm.get("reference", ""), "").strip() if norm.get("reference") else None,
            })
        except Exception as e:
            raise HTTPException(422, f"Bad CSV row {i}: {e}")
    n = await bank.import_statement(db, bank_account_id=account_id, rows=rows)
    await db.commit()
    return {"imported": n}


@router.get("/accounts/{account_id}/statement-lines")
async def statement_lines(account_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    rows = (await db.execute(select(BankStatementLine).where(BankStatementLine.bank_account_id == account_id)
                             .order_by(BankStatementLine.stmt_date))).scalars().all()
    return {"items": [{"id": r.id, "date": r.stmt_date, "description": r.description,
                       "amount": str(r.amount), "reference": r.reference, "status": r.status.value,
                       "matched_gl_line_id": r.matched_gl_line_id} for r in rows]}


@router.post("/reconciliations")
async def start_rec(body: ReconciliationStart, db: AsyncSession = Depends(get_db),
                    user: User = Depends(require_accounting)):
    rec = await bank.start_reconciliation(db, bank_account_id=body.bank_account_id,
                                          statement_date=body.statement_date,
                                          statement_balance=body.statement_balance, actor_user_id=user.id)
    res = await bank.compute_reconciliation(db, rec.id)
    await db.commit()
    return _stringify(res)


@router.get("/reconciliations/{rec_id}")
async def get_rec(rec_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    res = await bank.compute_reconciliation(db, rec_id)
    await db.commit()
    return _stringify(res)


@router.get("/reconciliations/{rec_id}/suggestions")
async def rec_suggestions(rec_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    rec = (await db.execute(select(Reconciliation).where(Reconciliation.id == rec_id))).scalar_one()
    return {"suggestions": await bank.suggest_matches(db, bank_account_id=rec.bank_account_id)}


@router.post("/reconciliations/{rec_id}/match")
async def rec_match(rec_id: str, body: MatchRequest, db: AsyncSession = Depends(get_db),
                    _: User = Depends(require_accounting)):
    await bank.apply_match(db, statement_line_id=body.statement_line_id, gl_line_id=body.gl_line_id)
    res = await bank.compute_reconciliation(db, rec_id)
    await db.commit()
    return _stringify(res)


@router.post("/reconciliations/{rec_id}/auto-match")
async def rec_auto(rec_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_accounting)):
    rec = (await db.execute(select(Reconciliation).where(Reconciliation.id == rec_id))).scalar_one()
    sugg = await bank.suggest_matches(db, bank_account_id=rec.bank_account_id)
    for s in sugg:
        await bank.apply_match(db, statement_line_id=s["statement_line_id"], gl_line_id=s["gl_line_id"])
    res = await bank.compute_reconciliation(db, rec_id)
    await db.commit()
    return {**_stringify(res), "matched": len(sugg)}


@router.post("/reconciliations/{rec_id}/complete")
async def rec_complete(rec_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_accounting)):
    rec = await bank.complete_reconciliation(db, rec_id, actor_user_id=user.id)
    await db.commit()
    return {"id": rec.id, "status": rec.status.value}
