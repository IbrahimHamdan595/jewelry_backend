from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core import gl
from app.core.coa_seed import seed_chart_of_accounts, post_opening_balances, SYSTEM_ACCOUNTS
from app.models import (
    GLAccount, GLPeriod, PeriodStatus, Denomination, GoldLot, LotSource, Karat,
    Supplier, SupplierBalance, DebtUnit,
)

D = Decimal


@pytest.mark.asyncio
async def test_seed_creates_all_system_accounts_idempotently(db):
    created = await seed_chart_of_accounts(db)
    assert created == len(SYSTEM_ACCOUNTS)

    keys = {a.system_key for a in (await db.execute(select(GLAccount))).scalars().all()}
    for required in ("CASH", "BANK", "METAL_INVENTORY", "AP", "METAL_AP",
                     "VAT_PAYABLE", "OPENING_BALANCE_EQUITY", "RETAINED_EARNINGS",
                     "SALES_REVENUE", "METAL_COGS", "FX_LOSS", "FX_GAIN"):
        assert required in keys

    # DUAL accounts carry grams + money.
    inv = (await db.execute(select(GLAccount).where(GLAccount.system_key == "METAL_INVENTORY"))).scalar_one()
    assert inv.denomination == Denomination.DUAL

    # Idempotent: second run creates nothing.
    created_again = await seed_chart_of_accounts(db)
    assert created_again == 0


@pytest.mark.asyncio
async def test_opening_balances_balances_with_equity(db):
    await seed_chart_of_accounts(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    # A gold lot on hand: 100g K21 costing $5000.
    db.add(GoldLot(karat=Karat.K21, weight_grams=D("100"), weight_remaining_grams=D("100"),
                   source=LotSource.SEED, cost_basis_usd=D("5000")))
    # A supplier we owe: $2000 cash + 30g K21.
    sup = Supplier(name="ACME Gold")
    db.add(sup)
    await db.flush()
    db.add(SupplierBalance(supplier_id=sup.id, unit=DebtUnit.CASH, karat="", balance=D("2000")))
    db.add(SupplierBalance(supplier_id=sup.id, unit=DebtUnit.GOLD, karat="K21", balance=D("30")))
    await db.flush()

    await post_opening_balances(
        db, as_of=date(2026, 6, 1), actor_user_id="u1",
        cash_lines=[{"system_key": "CASH", "amount": D("1500")}],
    )
    # Entry must balance in both dimensions (post_entry would have raised otherwise).
    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 1))
    assert tb["balanced"] is True
    assert tb["metal_balanced"] is True
    # Opening equity is the balancing plug.
    eq = next(a for a in tb["accounts"] if a["system_key"] == "OPENING_BALANCE_EQUITY")
    assert eq["net_base"] != D("0")


@pytest.mark.asyncio
async def test_opening_entry_hash_chain_verifies(db):
    """Regression: the hash chain must verify after an OPENING entry, which
    carries metal/DUAL lines. (A money-only entry verifying is not enough —
    metal lines round-trip grams + karat through the hash.)"""
    from app.core.audit_chain import verify_gl_chain
    from app.models import GLJournalEntry, GLJournalLine

    await seed_chart_of_accounts(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    db.add(GoldLot(karat=Karat.K21, weight_grams=D("100"), weight_remaining_grams=D("100"),
                   source=LotSource.SEED, cost_basis_usd=D("5000")))
    sup = Supplier(name="ACME Gold")
    db.add(sup)
    await db.flush()
    db.add(SupplierBalance(supplier_id=sup.id, unit=DebtUnit.GOLD, karat="K21", balance=D("30")))
    await db.flush()

    await post_opening_balances(
        db, as_of=date(2026, 6, 1), actor_user_id="u1",
        cash_lines=[{"system_key": "CASH", "amount": D("1500")}],
    )

    entries = (await db.execute(
        select(GLJournalEntry).order_by(GLJournalEntry.occurred_at, GLJournalEntry.id)
    )).scalars().all()
    rows = []
    for e in entries:
        lines = (await db.execute(
            select(GLJournalLine).where(GLJournalLine.entry_id == e.id).order_by(GLJournalLine.line_no)
        )).scalars().all()
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
    assert verify_gl_chain(rows)["status"] == "intact"
