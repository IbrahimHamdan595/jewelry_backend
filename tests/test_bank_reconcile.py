from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core import gl, bank
from app.core.coa_seed import seed_chart_of_accounts
from app.models import (
    GLPeriod, PeriodStatus, BankAccount, BankAccountType, BankStatementLine,
    StatementLineStatus, GLJournalLine, Reconciliation, ReconciliationStatus,
)

D = Decimal


async def _acct_with_movement(db):
    await seed_chart_of_accounts(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    await db.flush()
    a = await bank.create_bank_account(db, name="Acct", account_type=BankAccountType.BANK,
                                       currency="USD", bank_name=None, account_number=None, actor_user_id="u1")
    b = await bank.create_bank_account(db, name="Acct2", account_type=BankAccountType.BANK,
                                       currency="USD", bank_name=None, account_number=None, actor_user_id="u1")
    # Move $250 into account a (DR a / CR b) on 2026-06-04.
    await bank.post_transfer(db, from_account=b, to_account=a, amount=D("250"), dest_amount=None,
                             memo="seed", entry_date=date(2026, 6, 4), actor_user_id="u1", lbp_rate=D("89500"))
    return a


@pytest.mark.asyncio
async def test_import_and_automatch(db):
    a = await _acct_with_movement(db)
    n = await bank.import_statement(db, bank_account_id=a.id, rows=[
        {"stmt_date": date(2026, 6, 5), "description": "deposit", "amount": D("250"), "reference": "X1"},
        {"stmt_date": date(2026, 6, 5), "description": "noise", "amount": D("999"), "reference": "X2"},
    ])
    assert n == 2
    suggestions = await bank.suggest_matches(db, bank_account_id=a.id, window_days=5)
    assert len(suggestions) == 1  # only the 250 matches the DR line within window
    s = suggestions[0]
    await bank.apply_match(db, statement_line_id=s["statement_line_id"], gl_line_id=s["gl_line_id"])
    line = (await db.execute(select(BankStatementLine).where(BankStatementLine.id == s["statement_line_id"]))).scalar_one()
    assert line.status == StatementLineStatus.MATCHED
    # Idempotent re-apply.
    await bank.apply_match(db, statement_line_id=s["statement_line_id"], gl_line_id=s["gl_line_id"])
    # Conflicting re-match raises.
    other = (await db.execute(select(GLJournalLine).where(GLJournalLine.id != s["gl_line_id"]))).scalars().first()
    with pytest.raises(Exception):
        await bank.apply_match(db, statement_line_id=s["statement_line_id"], gl_line_id=other.id)


@pytest.mark.asyncio
async def test_reconciliation_compute_and_complete(db):
    a = await _acct_with_movement(db)
    await bank.import_statement(db, bank_account_id=a.id, rows=[
        {"stmt_date": date(2026, 6, 5), "description": "deposit", "amount": D("250"), "reference": "X1"},
    ])
    rec = await bank.start_reconciliation(db, bank_account_id=a.id, statement_date=date(2026, 6, 30),
                                          statement_balance=D("250"), actor_user_id="u1")
    res = await bank.compute_reconciliation(db, rec.id)
    # GL balance on account a = +250 (one DR). statement_balance 250 → difference 0.
    assert res["gl_balance"] == D("250.00")
    assert res["difference"] == D("0.00")
    sug = await bank.suggest_matches(db, bank_account_id=a.id)
    await bank.apply_match(db, statement_line_id=sug[0]["statement_line_id"], gl_line_id=sug[0]["gl_line_id"])
    res2 = await bank.compute_reconciliation(db, rec.id)
    assert res2["cleared_amount"] == D("250.00")
    done = await bank.complete_reconciliation(db, rec.id, actor_user_id="u1")
    assert done.status == ReconciliationStatus.COMPLETED
    ba = (await db.execute(select(BankAccount).where(BankAccount.id == a.id))).scalar_one()
    assert ba.last_reconciled_at is not None
