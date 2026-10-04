"""NEX-49 — a journal entry is reversed at most once, and a reversal is never
reversed. Guarded twice: gl.reverse_entry refuses with a 409 before the chain
head moves, and partial unique indexes on gl_journal_entries are the backstop
for the race the in-code check cannot see (and for auto-post double-posts)."""
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

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


# ── Index backstop: direct SQL ────────────────────────────────────────────────

_RAW_INSERT = text(
    "INSERT INTO gl_journal_entries "
    "(id, entry_no, entry_date, period_id, memo, source_type, source_id, reverses_entry_id, "
    " actor_user_id, occurred_at, prev_hash, entry_hash) "
    "VALUES (:id, :entry_no, '2026-06-05', :period_id, 'raw', :source_type, :source_id, "
    " :reverses_entry_id, 'u1', '2026-06-05 12:00:00', 'x', :entry_hash)"
)


def _raw_row(period_id, **over):
    uid = uuid4().hex
    return {"id": uid, "entry_no": f"JE-RAW-{uid[:8]}", "period_id": period_id,
            "source_type": gl.SOURCE_MANUAL, "source_id": None, "reverses_entry_id": None,
            "entry_hash": f"raw-{uid}", **over}


@pytest.mark.asyncio
async def test_index_rejects_duplicate_reversal_inserted_directly(db):
    accts = await _setup(db)
    orig = await _post(db, accts)
    other = await _post(db, accts)
    await gl.reverse_entry(db, original_entry_id=orig.id, actor_user_id="u1",
                           entry_date=date(2026, 6, 4))

    # Control: the same raw INSERT against a not-yet-reversed entry is accepted,
    # so the rejection below is the index and not a malformed statement.
    await db.execute(_RAW_INSERT, _raw_row(orig.period_id, source_type=gl.SOURCE_REVERSAL,
                                           source_id=other.id, reverses_entry_id=other.id))

    with pytest.raises(IntegrityError):
        await db.execute(_RAW_INSERT, _raw_row(orig.period_id, source_type=gl.SOURCE_REVERSAL,
                                               source_id=orig.id, reverses_entry_id=orig.id))


@pytest.mark.asyncio
async def test_index_rejects_duplicate_live_auto_post_source_inserted_directly(db):
    accts = await _setup(db)
    sale = await _post(db, accts, source_type=glp.SOURCE_ORDER, source_id="order-1")

    # Control: a different order is fine.
    await db.execute(_RAW_INSERT, _raw_row(sale.period_id, source_type=glp.SOURCE_ORDER,
                                           source_id="order-2"))

    with pytest.raises(IntegrityError):
        await db.execute(_RAW_INSERT, _raw_row(sale.period_id, source_type=glp.SOURCE_ORDER,
                                               source_id="order-1"))


# ── Index backstop: the lost race surfaces as a clean 409 ─────────────────────

@pytest.mark.asyncio
async def test_lost_reversal_race_is_409_and_leaves_chain_untouched(db):
    """Two requests can both pass reverse_entry's check before either commits.
    The loser then trips the index inside post_entry: that must be a 409, not
    an IntegrityError (500), and nothing of the attempt may persist."""
    accts = await _setup(db)
    orig = await _post(db, accts)
    orig_id = orig.id
    await gl.reverse_entry(db, original_entry_id=orig_id, actor_user_id="u1",
                           entry_date=date(2026, 6, 4))
    await db.commit()
    before = await _chain_state(db)

    # The loser's view: its check already passed, so it goes straight to the insert.
    with pytest.raises(HTTPException) as exc:
        await _post(db, accts, source_type=gl.SOURCE_REVERSAL, source_id=orig_id,
                    reverses_entry_id=orig_id, entry_date=date(2026, 6, 4))
    assert exc.value.status_code == 409

    await db.rollback()  # what closing the request's session does
    assert await _chain_state(db) == before


@pytest.mark.asyncio
async def test_lost_auto_post_race_is_409_and_leaves_chain_untouched(db):
    """find_live_entry runs before the chain-head lock, so two simultaneous
    posts of one source can both pass it. The index refuses the second."""
    accts = await _setup(db)
    await _post(db, accts, source_type=glp.SOURCE_ORDER, source_id="order-1")
    await db.commit()
    before = await _chain_state(db)

    with pytest.raises(HTTPException) as exc:
        await _post(db, accts, source_type=glp.SOURCE_ORDER, source_id="order-1")
    assert exc.value.status_code == 409

    await db.rollback()
    assert await _chain_state(db) == before


def test_only_the_two_duplicate_posting_indexes_become_a_409():
    """The suite runs on SQLite; production is Postgres, whose driver names the
    violated index instead of its columns. Pin both spellings, and that any
    other integrity failure is left to surface as the bug it is."""
    def _err(msg):
        return IntegrityError("INSERT INTO gl_journal_entries ...", {}, Exception(msg))

    pg = "<class 'asyncpg.exceptions.UniqueViolationError'>: duplicate key value violates unique constraint "
    assert "reversed" in gl._duplicate_posting_detail(_err(pg + '"uq_gl_entries_reverses_entry_id"'))
    assert "this source" in gl._duplicate_posting_detail(_err(pg + '"uq_gl_entries_live_source"'))
    assert "reversed" in gl._duplicate_posting_detail(
        _err("UNIQUE constraint failed: gl_journal_entries.reverses_entry_id"))
    assert "this source" in gl._duplicate_posting_detail(
        _err("UNIQUE constraint failed: gl_journal_entries.source_type, gl_journal_entries.source_id"))

    assert gl._duplicate_posting_detail(_err(pg + '"gl_journal_entries_entry_no_key"')) is None
    assert gl._duplicate_posting_detail(_err("UNIQUE constraint failed: gl_journal_entries.entry_hash")) is None
    assert gl._duplicate_posting_detail(_err("FOREIGN KEY constraint failed")) is None


# ── The indexes must never refuse a legitimate posting ────────────────────────

@pytest.mark.asyncio
async def test_sources_that_legitimately_repeat_are_not_blocked(db):
    accts = await _setup(db)
    # Manual entries: source_id is a free-text reference the accountant may reuse.
    await _post(db, accts, source_type=gl.SOURCE_MANUAL, source_id="ref-1")
    await _post(db, accts, source_type=gl.SOURCE_MANUAL, source_id="ref-1")
    # AR / AP / opening post with source_id NULL — many live entries per type.
    for st in ("AR_INVOICE", "AR_RECEIPT", "VENDOR_BILL", "VENDOR_PAYMENT", gl.SOURCE_OPENING):
        await _post(db, accts, source_type=st, source_id=None)
        await _post(db, accts, source_type=st, source_id=None)
    # Per-item partial refunds of ONE order: a distinct "<item>:<seq>" per event.
    await _post(db, accts, source_type=glp.SOURCE_ORDER, source_id="order-1")
    await _post(db, accts, source_type=glp.SOURCE_ORDER_REFUND, source_id="item-1:1")
    await _post(db, accts, source_type=glp.SOURCE_ORDER_REFUND, source_id="item-1:2")
    await _post(db, accts, source_type=glp.SOURCE_ORDER_REFUND, source_id="item-2:1")
    # The same id under different source types never collides.
    await _post(db, accts, source_type=glp.SOURCE_BUYBACK, source_id="shared-id")
    await _post(db, accts, source_type=glp.SOURCE_MELT, source_id="shared-id")

    assert (await _chain_state(db))[0] == 18


@pytest.mark.asyncio
async def test_year_close_entries_are_not_blocked(db):
    """YEAR_CLOSE posts with source_id NULL into a CLOSED December
    (allow_closed_period). Neither index may get in the way — including after a
    close has been reversed and the year is closed again."""
    accts = await _setup(db, months=(6,))
    db.add(GLPeriod(year=2026, period_no=12, status=PeriodStatus.CLOSED))
    await db.flush()

    first = await _post(db, accts, source_type="YEAR_CLOSE", source_id=None,
                        entry_date=date(2026, 12, 31), allow_closed_period=True)
    await gl.reverse_entry(db, original_entry_id=first.id, actor_user_id="u1",
                           entry_date=date(2026, 6, 30))
    second = await _post(db, accts, source_type="YEAR_CLOSE", source_id=None,
                         entry_date=date(2026, 12, 31), allow_closed_period=True)
    assert second.id != first.id and second.source_type == "YEAR_CLOSE"


def test_live_source_index_covers_exactly_the_auto_post_sources():
    """The index predicate is a literal list (an index cannot call Python). Pin
    it to gl_postings' SOURCE_* constants so a new auto-post source cannot be
    added without deciding whether it belongs in the index (and a migration)."""
    from app.models import GL_UNIQUE_LIVE_SOURCE_TYPES

    auto_post_sources = {v for k, v in vars(glp).items() if k.startswith("SOURCE_")}
    assert set(GL_UNIQUE_LIVE_SOURCE_TYPES) == auto_post_sources


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
