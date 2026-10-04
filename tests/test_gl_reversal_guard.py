"""NEX-49 — a journal entry is reversed at most once, and a reversal is never
reversed: gl.reverse_entry refuses with a 409 before the chain head moves."""
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core import gl
from app.core import gl_postings as glp
from app.models import (
    AccountType, Denomination, GLAccount, GLEntrySequence, GLJournalChainHead,
    GLJournalEntry, GLPeriod, NormalBalance, PeriodStatus,
)

D = Decimal


async def _setup(db, months=(6,)):
    for m in months:
        db.add(GLPeriod(year=2026, period_no=m, status=PeriodStatus.OPEN))
    cash = GLAccount(code="1000", name="Cash", type=AccountType.ASSET,
                     denomination=Denomination.MONEY, normal_balance=NormalBalance.DEBIT,
                     currency="USD", system_key="CASH")
    rev = GLAccount(code="4000", name="Sales", type=AccountType.INCOME,
                    denomination=Denomination.MONEY, normal_balance=NormalBalance.CREDIT,
                    currency="USD", system_key="SALES_REVENUE")
    inv = GLAccount(code="1200", name="Metal Inventory", type=AccountType.ASSET,
                    denomination=Denomination.DUAL, normal_balance=NormalBalance.DEBIT,
                    currency="USD", system_key="METAL_INVENTORY")
    cogs = GLAccount(code="5000", name="Metal COGS", type=AccountType.EXPENSE,
                     denomination=Denomination.DUAL, normal_balance=NormalBalance.DEBIT,
                     currency="USD", system_key="METAL_COGS")
    db.add_all([cash, rev, inv, cogs])
    await db.flush()
    return cash, rev, inv, cogs


def _sale_lines(accts):
    """A sale touching BOTH dimensions: 100 USD cash/revenue + 10g K21 out at cost 60."""
    cash, rev, inv, cogs = accts
    return [
        gl.GLLine(account_id=cash.id, denomination="MONEY", base_debit=D("100"), money_debit=D("100")),
        gl.GLLine(account_id=rev.id, denomination="MONEY", base_credit=D("100"), money_credit=D("100")),
        gl.GLLine(account_id=cogs.id, denomination="DUAL", base_debit=D("60"),
                  metal_debit_grams=D("10.000"), karat="K21"),
        gl.GLLine(account_id=inv.id, denomination="DUAL", base_credit=D("60"),
                  metal_credit_grams=D("10.000"), karat="K21"),
    ]


async def _post(db, accts, *, source_type=gl.SOURCE_MANUAL, source_id=None,
                entry_date=date(2026, 6, 3), **kw):
    return await gl.post_entry(
        db, entry_date=entry_date, memo="sale", source_type=source_type, source_id=source_id,
        actor_user_id="u1", lines=_sale_lines(accts), **kw,
    )


async def _chain_state(db):
    """Everything a refused attempt must leave untouched: the head's row count
    and latest hash, the number of entries, and the per-day entry_no counters
    (a burned JE number would be a visible gap)."""
    head = (await db.execute(select(GLJournalChainHead).where(GLJournalChainHead.id == 1))).scalar_one()
    entries = (await db.execute(select(func.count()).select_from(GLJournalEntry))).scalar_one()
    seqs = (await db.execute(select(func.coalesce(func.sum(GLEntrySequence.last_seq), 0)))).scalar_one()
    return head.row_count, head.latest_entry_hash, entries, seqs


# ── In-code guard (gl.reverse_entry) ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_second_reversal_refused_409_and_chain_untouched(db):
    accts = await _setup(db)
    orig = await _post(db, accts)
    first = await gl.reverse_entry(db, original_entry_id=orig.id, actor_user_id="u1",
                                   entry_date=date(2026, 6, 4))
    before = await _chain_state(db)
    assert before[0] == 2 and before[1] == first.entry_hash

    with pytest.raises(HTTPException) as exc:
        await gl.reverse_entry(db, original_entry_id=orig.id, actor_user_id="u1",
                               entry_date=date(2026, 6, 4))
    assert exc.value.status_code == 409
    assert orig.entry_no in exc.value.detail and first.entry_no in exc.value.detail

    # Refused BEFORE the head advanced: no gap in the hash chain, no burned JE number.
    assert await _chain_state(db) == before


@pytest.mark.asyncio
async def test_reversing_a_reversal_refused_and_chain_untouched(db):
    accts = await _setup(db)
    orig = await _post(db, accts)
    rev = await gl.reverse_entry(db, original_entry_id=orig.id, actor_user_id="u1",
                                 entry_date=date(2026, 6, 4))
    before = await _chain_state(db)

    with pytest.raises(HTTPException) as exc:
        await gl.reverse_entry(db, original_entry_id=rev.id, actor_user_id="u1",
                               entry_date=date(2026, 6, 4))
    assert exc.value.status_code == 409
    assert await _chain_state(db) == before


@pytest.mark.asyncio
async def test_single_reversal_nets_original_to_zero_in_both_dimensions(db):
    accts = await _setup(db)
    orig = await _post(db, accts)
    rev = await gl.reverse_entry(db, original_entry_id=orig.id, actor_user_id="u1",
                                 entry_date=date(2026, 6, 4))
    assert rev.reverses_entry_id == orig.id and rev.source_type == gl.SOURCE_REVERSAL

    tb = await gl.compute_trial_balance(db, as_of=date(2026, 6, 30))
    assert tb["balanced"] is True and tb["metal_balanced"] is True
    assert len(tb["accounts"]) == 4
    for a in tb["accounts"]:
        assert a["net_base"] == D("0.00"), a["code"]
        for k, v in a["metal_by_karat"].items():
            assert v["net_grams"] == D("0.000"), (a["code"], k)
    # The metal dimension really was exercised (not vacuously zero).
    by_key = {a["system_key"]: a for a in tb["accounts"]}
    assert by_key["METAL_INVENTORY"]["metal_by_karat"]["K21"]["debit_grams"] == D("10.000")
    assert by_key["METAL_INVENTORY"]["metal_by_karat"]["K21"]["credit_grams"] == D("10.000")


@pytest.mark.asyncio
async def test_reversal_may_land_in_a_later_period_and_still_counts(db):
    """A reversal is booked when it happens, so it legitimately lands in a
    different period from the original. The guard must not depend on period."""
    accts = await _setup(db, months=(6, 7, 8))
    orig = await _post(db, accts, entry_date=date(2026, 6, 3))
    rev = await gl.reverse_entry(db, original_entry_id=orig.id, actor_user_id="u1",
                                 entry_date=date(2026, 7, 10))
    assert rev.period_id != orig.period_id

    with pytest.raises(HTTPException) as exc:
        await gl.reverse_entry(db, original_entry_id=orig.id, actor_user_id="u1",
                               entry_date=date(2026, 8, 1))  # a third period
    assert exc.value.status_code == 409


# ── Auto-post path: a second full void/refund of one order ────────────────────

@pytest.mark.asyncio
async def test_second_full_void_of_an_order_is_refused_not_double_reversed(db):
    from app.core.coa_seed import seed_chart_of_accounts
    from app.models import Karat, Order, OrderItem, OrderItemKind, PaymentMethod, Settings
    from tests.conftest import BOOK_DATETIME

    await seed_chart_of_accounts(db)
    db.add(GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN))
    order = Order(
        order_number="ORD-1", cashier_id="u1", payment_method=PaymentMethod.CASH,
        subtotal=D("100"), vat_percent=D("11"), vat_amount=D("11"),
        discount_percent=D("0"), discount_amount=D("0"),
        total_usd=D("111"), total_lbp=D("9934500"), lbp_exchange_rate=D("89500"),
        created_at=BOOK_DATETIME,
    )
    order.items = [OrderItem(
        item_kind=OrderItemKind.COIN, product_code="C1", product_name="Coin", karat=Karat.K21,
        weight_grams=D("10.000"), gold_rate_at_sale=D("60.00"), margin_percent=D("0"),
        making_charge=D("0"), final_price=D("100"), quantity=1,
    )]
    db.add(order)
    await db.flush()
    cfg = Settings(id="singleton", accounting_auto_post_enabled=True, vat_percent=D("11"),
                   lbp_exchange_rate=D("89500"))

    await glp.post_sale(db, order, cfg, "u1")
    assert await glp.post_order_refund(db, order, cfg, "u1", refunded_item=None) is not None
    before = await _chain_state(db)

    with pytest.raises(HTTPException) as exc:
        await glp.post_order_refund(db, order, cfg, "u1", refunded_item=None)
    assert exc.value.status_code == 409
    assert await _chain_state(db) == before
