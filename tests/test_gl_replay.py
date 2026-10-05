"""NEX-52 — historical GL replay (app/core/gl_replay.py + scripts/replay_gl_history.py).

The general ledger shipped switched off, so the shop has months of activity in
the inventory ledger and nothing in the GL. The replay posts that history
through the SAME mappers the live paths use, at the original dates.

What these tests pin, in the order the ticket lists it:
  • dry run is the default and leaves no trace; --execute writes;
  • the whole run is one transaction — a failure anywhere leaves nothing behind
    and both hash chains untouched;
  • a second run posts nothing;
  • every mapper-backed document type is covered, in chronological order;
  • historical COGS comes from what the order stored, never today's rate;
  • the auto-post flag is neither required nor changed.

Fixtures are built with explicit 2026 dates (March → June) so the periods the
replay opens are deterministic — nothing here depends on the day the suite runs,
except the one end-to-end test that drives the real endpoints.
"""
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, event, func, select

from app.core import gl_postings, gl_replay
from app.core.audit_chain import GENESIS_HASH, compute_ledger_entry_hash
from app.core.coa_seed import seed_chart_of_accounts
from app.core.gl import compute_trial_balance
from app.core.ledger import EVENT_ORDER_ITEM_REFUND
from app.models import (
    AdjustmentReason, AdjustmentTarget, ARInvoice, ARInvoiceStatus, ARReceipt, BuybackKind,
    BuybackPriceMode, CoinType, Customer, DebtUnit, GLAccount, GLEntrySequence,
    GLJournalChainHead, GLJournalEntry, GLJournalLine, GLPeriod, GoldLot, GoldRateHistory,
    InventoryLedger, InventoryLedgerChainHead, Karat, LotSource, ManualAdjustment, MarginMode,
    Order, OrderItem, OrderItemKind, OrderStatus, PaymentMethod, PeriodStatus, Product,
    ProductStatus, Role, Settings, Supplier, SupplierItemKind, SupplierPayment,
    SupplierPurchase, SupplierPurchaseItem, SupplierPurchaseMode, User, WalkinBuyback,
)

D = Decimal
ADMIN = "u-admin"
CASHIER = "u-cashier"
EVERYTHING = date(2999, 12, 31)  # trial-balance cutoff that includes every entry


def _at(month: int, day: int) -> datetime:
    return datetime(2026, month, day, 12, 0, 0)


# ── fixtures ──────────────────────────────────────────────────────────────────

async def _base(db, *, flag: bool = False, seed_coa: bool = True) -> None:
    """Users + settings + chart of accounts — and, like production, zero periods."""
    db.add_all([
        User(id=ADMIN, email="owner@x.com", name="Owner", password_hash="x",
             role=Role.ADMIN, is_active=True),
        User(id=CASHIER, email="till@x.com", name="Till", password_hash="x",
             role=Role.CASHIER, is_active=True),
        Settings(id="singleton", accounting_auto_post_enabled=flag),
    ])
    if seed_coa:
        await seed_chart_of_accounts(db)
    await db.commit()


def _sale(number: str, when: datetime, *, qty: int = 1, discount_pct: D = D("0"),
          status: OrderStatus = OrderStatus.COMPLETED,
          payment: PaymentMethod = PaymentMethod.CASH, **extra) -> Order:
    """A K21 10 g coin line at 700 a piece, gold at 60/g when it was sold.
    Metal COGS per piece = 60 × 10 × 0.875 = 525.00."""
    subtotal = D("700") * qty
    vat = (subtotal * D("11") / D(100)).quantize(D("0.01"))
    discount = (subtotal * discount_pct / D(100)).quantize(D("0.01"))
    order = Order(
        id=f"o-{number}", order_number=number, status=status, payment_method=payment,
        cashier_id=CASHIER, subtotal=subtotal, vat_percent=D("11"), vat_amount=vat,
        discount_percent=discount_pct, discount_amount=discount,
        total_usd=subtotal + vat - discount, total_lbp=D("0"), lbp_exchange_rate=D("89500"),
        created_at=when, **extra,
    )
    order.items = [OrderItem(
        id=f"it-{number}", item_kind=OrderItemKind.COIN, quantity=qty, product_code="C21",
        product_name="Coin", karat=Karat.K21, weight_grams=D("10.000"),
        gold_rate_at_sale=D("60.00"), margin_percent=D("0"), making_charge=D("0"),
        final_price=subtotal,
    )]
    return order


async def _ledger_at(db, when: datetime, *, event_type: str, ref_type: str, ref_id: str,
                     payload: dict) -> None:
    """ledger.record() with a chosen timestamp — a back-dated but properly
    chained inventory-ledger row, standing in for one written months ago."""
    head = (await db.execute(
        select(InventoryLedgerChainHead).where(InventoryLedgerChainHead.id == 1)
    )).scalar_one()
    entry_hash = compute_ledger_entry_hash(prev_hash=head.latest_entry_hash, fields={
        "event_type": event_type, "actor_user_id": ADMIN, "occurred_at": when,
        "ref_type": ref_type, "ref_id": ref_id, "payload": payload,
    })
    db.add(InventoryLedger(event_type=event_type, actor_user_id=ADMIN, occurred_at=when,
                           ref_type=ref_type, ref_id=ref_id, payload=payload,
                           prev_hash=head.latest_entry_hash, entry_hash=entry_hash))
    head.latest_entry_hash = entry_hash
    head.row_count = head.row_count + 1
    await db.flush()


async def _refund_line(db, order: Order, when: datetime, *, qty: int, record_event: bool = True) -> None:
    """Leave `order` exactly as POST /orders/{id}/items/{item}/refund leaves it:
    the line's refunded_* counters advanced, the HEADER totals overwritten with
    what remains, and one ORDER_ITEM_REFUND ledger event."""
    item = order.items[0]
    value = (item.final_price / item.quantity * qty).quantize(D("0.01"))
    item.refunded_qty += qty
    item.refunded_amount = (item.refunded_amount or D("0")) + value
    item.refunded_at = when
    remaining = item.final_price - item.refunded_amount
    total_before = order.total_usd
    order.subtotal = remaining
    order.vat_amount = (remaining * order.vat_percent / D(100)).quantize(D("0.01"))
    order.discount_amount = (remaining * order.discount_percent / D(100)).quantize(D("0.01"))
    order.total_usd = remaining + order.vat_amount - order.discount_amount
    order.status = (OrderStatus.REFUNDED if item.refunded_qty >= item.quantity
                    else OrderStatus.PARTIALLY_REFUNDED)
    await db.flush()
    if record_event:
        await _ledger_at(db, when, event_type=EVENT_ORDER_ITEM_REFUND, ref_type="order_item",
                         ref_id=item.id, payload={
                             "order_id": order.id, "order_number": order.order_number,
                             "item_kind": item.item_kind.value, "code": item.product_code,
                             "refunded_qty": qty, "refunded_qty_total": item.refunded_qty,
                             "line_quantity": item.quantity, "refund_amount": str(value),
                             "cash_refunded_usd": str(total_before - order.total_usd),
                             "order_status_after": order.status.value,
                             "order_total_usd_after": str(order.total_usd),
                         })


async def _history(db) -> None:
    """One of every document the posting mappers support, March → June 2026,
    all recorded while the auto-post flag was off (so: no GL entries)."""
    db.add(Supplier(id="sup1", name="ACME"))
    await db.flush()

    # 03-05  mixed supplier purchase: 50 g K21 on the metal account (cost 3000)
    #        plus 1000 USD of product, 300 of it paid in cash on the day.
    db.add(GoldLot(id="lot-sup", karat=Karat.K21, weight_grams=D("50.000"),
                   weight_remaining_grams=D("25.000"), source=LotSource.SUPPLIER,
                   source_ref_type="supplier_purchase", source_ref_id="pur1",
                   cost_basis_usd=D("3000"), acquired_at=_at(3, 5)))
    purchase = SupplierPurchase(
        id="pur1", supplier_id="sup1", payment_mode=SupplierPurchaseMode.MIXED,
        total_cash_due=D("1000"), total_grams_due_by_karat={"K21": "50.000"},
        cash_paid_at_creation=D("300"), grams_paid_at_creation_by_karat={},
        created_by_user_id=ADMIN, occurred_at=_at(3, 5),
    )
    purchase.items = [
        SupplierPurchaseItem(item_kind=SupplierItemKind.PURE_GOLD, karat=Karat.K21,
                             weight_grams=D("50.000"), unit_cost_usd=D("3000")),
        SupplierPurchaseItem(item_kind=SupplierItemKind.PRODUCT, unit_cost_usd=D("1000")),
    ]
    db.add(purchase)

    # 03-10  plain completed sale.
    db.add(_sale("S1", _at(3, 10)))
    # 03-12  sale, voided the FOLLOWING month (04-02).
    db.add(_sale("S2", _at(3, 12), status=OrderStatus.VOIDED, voided_at=_at(4, 2),
                 voided_by=ADMIN, void_reason="wrong customer"))
    # 03-20  walk-in buyback: 10 g K21 for 500 cash.
    db.add(WalkinBuyback(
        id="bb1", occurred_at=_at(3, 20), seller_name="Seller", seller_phone="1",
        cashier_id=CASHIER, kind=BuybackKind.PURE_GOLD, weight_grams=D("10.000"),
        karat=Karat.K21, buy_price_usd=D("500"), gold_rate_at_buy=D("60"),
        price_mode=BuybackPriceMode.MANUAL,
    ))
    # 04-08 / 04-09  supplier repayments: 200 cash, then 20 g K21.
    db.add(SupplierPayment(id="pay-cash", supplier_id="sup1", paid_at=_at(4, 8),
                           unit=DebtUnit.CASH, amount=D("200"), paid_by_user_id=ADMIN))
    db.add(SupplierPayment(id="pay-gold", supplier_id="sup1", paid_at=_at(4, 9),
                           unit=DebtUnit.GOLD, karat=Karat.K21, amount=D("20.000"),
                           paid_by_user_id=ADMIN))
    # 04-15  two coins at 10% off; ONE refunded on 05-03 (header totals rewritten).
    s3 = _sale("S3", _at(4, 15), qty=2, discount_pct=D("10"))
    db.add(s3)
    await db.flush()
    await _refund_line(db, s3, _at(5, 3), qty=1)
    # 05-10  melt: a 20 g K21 piece refined into a 17 g K24 lot (cost 1200).
    db.add(Product(id="p-melt", code="P-MELT", name_en="Old ring", category="Rings",
                   karat=Karat.K21, weight_grams=D("20.000"), margin_percent=D("10"),
                   making_charge=D("0"), status=ProductStatus.MELTED, on_hand_qty=1))
    db.add(GoldLot(id="lot-melt", karat=Karat.K24, weight_grams=D("17.000"),
                   weight_remaining_grams=D("17.000"), source=LotSource.MELT,
                   source_ref_type="product", source_ref_id="p-melt",
                   cost_basis_usd=D("1200"), acquired_at=_at(5, 10)))
    # 05-20  5 g of the supplier lot written off (cost 3000 × 5/50 = 300).
    db.add(ManualAdjustment(id="adj1", target_type=AdjustmentTarget.LOT, target_id="lot-sup",
                            delta=D("-5.000"), reason=AdjustmentReason.LOSS,
                            notes="lost in polishing", occurred_at=_at(5, 20),
                            actor_user_id=ADMIN))
    # 06-01  sale refunded whole through the legacy endpoint: status only, no date.
    db.add(_sale("S4", _at(6, 1), status=OrderStatus.REFUNDED))
    await db.commit()


# The 13 entries the history above must produce, in chain order.
EXPECTED_CHAIN = [
    ("SUPPLIER_PURCHASE", date(2026, 3, 5)),
    ("ORDER", date(2026, 3, 10)),            # S1
    ("ORDER", date(2026, 3, 12)),            # S2
    ("BUYBACK", date(2026, 3, 20)),
    ("REVERSAL", date(2026, 4, 2)),          # S2 voided
    ("SUPPLIER_PAYMENT", date(2026, 4, 8)),
    ("SUPPLIER_PAYMENT", date(2026, 4, 9)),
    ("ORDER", date(2026, 4, 15)),            # S3
    ("ORDER_REFUND", date(2026, 5, 3)),      # S3 line refund
    ("MELT", date(2026, 5, 10)),
    ("ADJUSTMENT", date(2026, 5, 20)),
    ("ORDER", date(2026, 6, 1)),             # S4
    ("REVERSAL", date(2026, 6, 1)),          # S4 refunded whole (undated → sale date)
]


async def _books(db) -> dict:
    """Everything a replay is allowed to touch, read fresh from the DB."""
    async def count(model) -> int:
        return (await db.execute(select(func.count()).select_from(model))).scalar_one()

    periods = (await db.execute(
        select(GLPeriod.year, GLPeriod.period_no, GLPeriod.status)
        .order_by(GLPeriod.year, GLPeriod.period_no)
    )).all()
    return {
        "entries": await count(GLJournalEntry),
        "lines": await count(GLJournalLine),
        "sequences": await count(GLEntrySequence),
        "periods": [(y, m, s.value) for y, m, s in periods],
        "gl_head": tuple((await db.execute(
            select(GLJournalChainHead.latest_entry_hash, GLJournalChainHead.row_count))).one()),
        "ledger_head": tuple((await db.execute(
            select(InventoryLedgerChainHead.latest_entry_hash,
                   InventoryLedgerChainHead.row_count))).one()),
        "flag": (await db.execute(select(Settings.accounting_auto_post_enabled))).scalar_one(),
        "invoice_links": await count(ARInvoice) - (await db.execute(
            select(func.count()).select_from(ARInvoice).where(ARInvoice.gl_entry_id.is_(None))
        )).scalar_one(),
    }


async def _chain(db) -> list[GLJournalEntry]:
    return list((await db.execute(
        select(GLJournalEntry).order_by(GLJournalEntry.occurred_at, GLJournalEntry.id)
    )).scalars().all())


async def _accounts(db) -> dict:
    tb = await compute_trial_balance(db, as_of=EVERYTHING)
    assert tb["balanced"] is True and tb["metal_balanced"] is True
    return {a["system_key"]: a for a in tb["accounts"] if a["system_key"]}


async def _verify(db) -> dict:
    """The exact check the accounting screens call: GET /accounting/ledger/verify."""
    from app.api.accounting import verify
    return await verify(db=db, _=None)


# ── dry run ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dry_run_is_the_default_and_writes_nothing(db):
    await _base(db)
    await _history(db)
    before = await _books(db)

    report = await gl_replay.run_replay(db, actor_user_id=ADMIN)  # no execute=

    assert report.executed is False
    assert report.entries_posted == 13
    assert await _books(db) == before          # not one row, period, hash or counter
    assert before["entries"] == 0 and before["periods"] == []
    assert before["gl_head"] == (GENESIS_HASH, 0)


@pytest.mark.asyncio
async def test_dry_run_reports_counts_totals_and_the_periods_it_would_open(db):
    await _base(db)
    await _history(db)

    report = await gl_replay.run_replay(db, actor_user_id=ADMIN)
    kinds = report.kinds

    assert (kinds[gl_replay.KIND_SALE].documents, kinds[gl_replay.KIND_SALE].posted) == (4, 4)
    # S1 1302 + S2 1302 + S3 2604 + S4 1302 (cash/discount debits + metal COGS)
    assert kinds[gl_replay.KIND_SALE].base_total == D("6510.00")
    assert kinds[gl_replay.KIND_SALE].grams_by_karat == {"K21": D("50.000")}
    assert kinds[gl_replay.KIND_SALE_REVERSAL].posted == 2
    assert kinds[gl_replay.KIND_SALE_REVERSAL].base_total == D("2604.00")
    assert kinds[gl_replay.KIND_LINE_REFUND].posted == 1
    assert kinds[gl_replay.KIND_LINE_REFUND].base_total == D("1302.00")
    assert kinds[gl_replay.KIND_SUPPLIER_PURCHASE].posted == 1
    assert kinds[gl_replay.KIND_SUPPLIER_PURCHASE].base_total == D("4000.00")
    assert kinds[gl_replay.KIND_SUPPLIER_PAYMENT].posted == 2
    assert kinds[gl_replay.KIND_SUPPLIER_PAYMENT].base_total == D("200.00")
    assert kinds[gl_replay.KIND_SUPPLIER_PAYMENT].grams_by_karat == {"K21": D("20.000")}
    assert kinds[gl_replay.KIND_BUYBACK].posted == 1
    assert kinds[gl_replay.KIND_BUYBACK].base_total == D("500.00")
    assert kinds[gl_replay.KIND_MELT].posted == 1
    assert kinds[gl_replay.KIND_MELT].grams_by_karat == {"K21": D("20.000"), "K24": D("17.000")}
    assert kinds[gl_replay.KIND_ADJUSTMENT].posted == 1
    assert kinds[gl_replay.KIND_ADJUSTMENT].base_total == D("300.00")
    assert sum(k.posted for k in kinds.values()) == report.entries_posted == 13

    # ensure_period opens every missing month as OPEN — the operator must be told.
    assert report.periods_created == ["2026-03", "2026-04", "2026-05", "2026-06"]
    assert report.trial_balance["balanced"] is True
    assert report.trial_balance["metal_balanced"] is True
    assert report.auto_post_enabled is False

    text = gl_replay.format_report(report)
    assert "DRY RUN" in text and "nothing was written" in text.lower()
    for needle in ("Sales", "Supplier purchases", "Walk-in buybacks", "Melts",
                   "2026-03", "2026-06", "6,510.00", "OPEN"):
        assert needle in text, f"{needle!r} missing from the printed report"


# ── execute ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_posts_every_document_type_at_its_original_date_in_order(db):
    await _base(db)
    await _history(db)

    report = await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)

    assert report.executed is True and report.entries_posted == 13
    chain = await _chain(db)
    # Chain order == chronological order of the original documents.
    assert [(e.source_type, e.entry_date) for e in chain] == EXPECTED_CHAIN
    assert [e.entry_date for e in chain] == sorted(e.entry_date for e in chain)
    # Every mapper's source type is represented, each pointing at its document.
    by_source = {(e.source_type, e.source_id) for e in chain}
    assert {("ORDER", "o-S1"), ("ORDER", "o-S2"), ("ORDER", "o-S3"), ("ORDER", "o-S4"),
            ("ORDER_REFUND", "it-S3:1"), ("SUPPLIER_PURCHASE", "pur1"),
            ("SUPPLIER_PAYMENT", "pay-cash"), ("SUPPLIER_PAYMENT", "pay-gold"),
            ("BUYBACK", "bb1"), ("MELT", "lot-melt"), ("ADJUSTMENT", "adj1")} <= by_source
    assert all(e.actor_user_id == ADMIN for e in chain)  # the operator, not the cashier

    # Periods: created for the past months, and left OPEN for review.
    assert (await _books(db))["periods"] == [
        (2026, 3, "OPEN"), (2026, 4, "OPEN"), (2026, 5, "OPEN"), (2026, 6, "OPEN")]

    # Balanced in money AND metal, and the hash chain verifies end to end.
    accts = await _accounts(db)
    # −300 purchase +777 S1 +0 S2 −500 buyback −200 payment +707 S3 +0 S4
    assert accts["CASH"]["net_base"] == D("484.00")
    assert accts["AP"]["net_base"] == D("-500.00")                       # 700 owed − 200 paid
    assert accts["METAL_AP"]["metal_by_karat"]["K21"]["net_grams"] == D("-30.000")
    assert accts["ADJUSTMENT_EXPENSE"]["net_base"] == D("300.00")
    assert accts["METAL_INVENTORY"]["metal_by_karat"]["K24"]["net_grams"] == D("17.000")
    check = await _verify(db)
    assert check["status"] == "intact" and check["head_matches"] is True
    assert check["head_row_count"] == 13


@pytest.mark.asyncio
async def test_execute_leaves_a_single_audit_marker_on_the_inventory_ledger(db):
    """Back-dated entries must be explainable: one ledger row says who ran the
    replay and what it posted."""
    await _base(db)
    await _history(db)
    await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)

    rows = (await db.execute(
        select(InventoryLedger).where(InventoryLedger.event_type == "GL_HISTORY_REPLAYED")
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].actor_user_id == ADMIN
    assert rows[0].payload["entries_posted"] == 13
    assert rows[0].payload["periods_created"] == ["2026-03", "2026-04", "2026-05", "2026-06"]


@pytest.mark.asyncio
@pytest.mark.parametrize("execute", [False, True])
async def test_replay_only_ever_inserts_into_the_append_only_tables(db, execute):
    """In production, Postgres triggers reject any UPDATE or DELETE on the
    journal and on the inventory ledger. SQLite has no such triggers, so this
    watches the SQL itself: against those tables the replay may only INSERT —
    on a dry run too (it is undone by ROLLBACK, never by deleting)."""
    await _base(db)
    await _history(db)
    statements: list[str] = []

    def spy(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()).upper())

    engine = db.bind.sync_engine
    event.listen(engine, "before_cursor_execute", spy)
    try:
        await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=execute)
    finally:
        event.remove(engine, "before_cursor_execute", spy)

    protected = ("GL_JOURNAL_ENTRIES", "GL_JOURNAL_LINES", "INVENTORY_LEDGER")
    offenders = [
        st for st in statements
        if st.startswith(("UPDATE", "DELETE")) and st.split()[1 if st.startswith("UPDATE") else 2] in protected
    ]
    assert offenders == []
    assert any(st.startswith("INSERT INTO GL_JOURNAL_ENTRIES") for st in statements)


@pytest.mark.asyncio
async def test_second_run_posts_nothing_new(db):
    await _base(db)
    await _history(db)
    first = await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)
    after_first = await _books(db)

    second = await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)

    assert first.entries_posted == 13 and second.entries_posted == 0
    assert sum(k.already_posted for k in second.kinds.values()) == 13
    assert second.periods_created == []
    # Nothing moved: no entry, no chain advance, not even an audit marker.
    assert await _books(db) == after_first
    # …and a dry run afterwards agrees there is nothing left to do.
    assert (await gl_replay.run_replay(db, actor_user_id=ADMIN)).entries_posted == 0


# ── one transaction ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_failure_halfway_leaves_nothing_behind(db):
    """May is CLOSED, so the replay posts March and April and then hits a wall.
    None of it may survive — entries, auto-opened periods, chain heads."""
    await _base(db)
    await _history(db)
    db.add(GLPeriod(year=2026, period_no=5, status=PeriodStatus.CLOSED))
    await db.commit()
    before = await _books(db)

    with pytest.raises(gl_replay.ReplayError) as exc:
        await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)

    # The operator is told WHICH document hit WHAT.
    assert "S3" in str(exc.value) and "2026-05" in str(exc.value) and "CLOSED" in str(exc.value)
    assert await _books(db) == before
    assert before["entries"] == 0 and before["periods"] == [(2026, 5, "CLOSED")]
    assert before["gl_head"] == (GENESIS_HASH, 0)


@pytest.mark.asyncio
async def test_an_unexpected_error_on_the_last_document_also_rolls_everything_back(db, monkeypatch):
    await _base(db)
    await _history(db)
    before = await _books(db)

    async def boom(*args, **kwargs):
        raise RuntimeError("database went away")

    # The S4 reversal is the very last step: 12 entries are already flushed.
    real = gl_postings.post_order_refund

    async def fail_on_s4(db_, order, *args, **kwargs):
        if order.order_number == "S4":
            return await boom()
        return await real(db_, order, *args, **kwargs)

    monkeypatch.setattr(gl_postings, "post_order_refund", fail_on_s4)
    with pytest.raises(RuntimeError, match="database went away"):
        await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)

    assert await _books(db) == before


# ── the flag ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [False, True])
async def test_replay_ignores_the_auto_post_flag_and_never_changes_it(db, flag):
    await _base(db, flag=flag)
    await _history(db)

    report = await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)

    assert report.entries_posted == 13
    assert report.auto_post_enabled is flag
    assert (await _books(db))["flag"] is flag
    assert (await db.execute(
        select(func.count()).select_from(InventoryLedger)
        .where(InventoryLedger.event_type == "SETTINGS_CHANGED")
    )).scalar_one() == 0


# ── COGS comes from the order, not from today ─────────────────────────────────

@pytest.mark.asyncio
async def test_cogs_uses_the_gold_rate_stored_on_the_order_not_todays(db):
    await _base(db)
    db.add(_sale("S1", _at(3, 10)))                        # sold with gold at 60/g
    db.add(GoldRateHistory(rate_24k=D("999"), source="test"))  # gold is 999/g today
    await db.commit()

    await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)

    accts = await _accounts(db)
    assert accts["METAL_COGS"]["base_debit"] == D("525.00")     # 60 × 10 g × 0.875
    assert accts["METAL_COGS"]["base_debit"] != D("8741.25")    # 999 × 10 g × 0.875
    assert accts["METAL_COGS"]["metal_by_karat"]["K21"]["net_grams"] == D("10.000")


def _product_sale(cost_now: D) -> list:
    """A PRODUCT line whose checkout snapshotted a 300.00 metal cost."""
    product = Product(id="p1", code="P-1", name_en="Ring", category="Rings", karat=Karat.K21,
                      weight_grams=D("5.000"), margin_percent=D("10"), making_charge=D("0"),
                      status=ProductStatus.SOLD, on_hand_qty=0, cost_basis_usd=cost_now)
    order = _sale("P1", _at(3, 10))
    item = order.items[0]
    item.item_kind = OrderItemKind.PRODUCT
    item.product_id = "p1"
    item.weight_grams = D("5.000")
    item.cost_basis_usd = D("300.00")       # OrderItem.cost_basis_usd — the sale-time snapshot
    return [product, order]


@pytest.mark.asyncio
async def test_cogs_equals_the_cost_snapshot_stored_on_the_order_line(db):
    await _base(db)
    db.add_all(_product_sale(cost_now=D("300.00")))
    await db.commit()

    await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)

    accts = await _accounts(db)
    assert accts["METAL_COGS"]["base_debit"] == D("300.00")   # the stored snapshot, not 60×5×0.875


@pytest.mark.asyncio
async def test_replay_refuses_to_post_cogs_that_disagrees_with_the_stored_snapshot(db):
    """If the cost the mapper would post has drifted from what the order line
    stored at checkout, the replay stops rather than rewrite history."""
    await _base(db)
    db.add_all(_product_sale(cost_now=D("999.00")))           # product cost changed since
    await db.commit()
    before = await _books(db)

    with pytest.raises(gl_replay.ReplayError) as exc:
        await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)

    assert "P1" in str(exc.value) and "300.00" in str(exc.value) and "999.00" in str(exc.value)
    assert await _books(db) == before


# ── refunds and voids ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_line_refunded_order_is_replayed_from_its_original_totals(db):
    """A line refund overwrites the order header with what REMAINS. The replay
    must post the sale as it was sold, then the refund — so the books end up
    agreeing with the order as it stands today."""
    await _base(db)
    order = _sale("S3", _at(4, 15), qty=2, discount_pct=D("10"))   # 1400 − 140 + 154 = 1414
    db.add(order)
    await db.flush()
    await _refund_line(db, order, _at(5, 3), qty=1)                # header now reads 707
    await db.commit()
    assert order.total_usd == D("707.00")

    await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)

    sale, refund = await _chain(db)
    assert (sale.source_type, sale.entry_date) == ("ORDER", date(2026, 4, 15))
    # Same source id the live refund path uses → the two can never double-post.
    assert (refund.source_type, refund.source_id) == ("ORDER_REFUND", "it-S3:1")
    assert refund.entry_date == date(2026, 5, 3)
    accts = await _accounts(db)
    assert accts["CASH"]["base_debit"] == D("1414.00")             # the ORIGINAL sale
    assert accts["CASH"]["net_base"] == D("707.00") == order.total_usd
    assert accts["SALES_REVENUE"]["net_base"] == D("-700.00")
    assert accts["SALES_DISCOUNTS"]["net_base"] == D("70.00")
    assert accts["VAT_PAYABLE"]["net_base"] == D("-77.00")
    assert accts["METAL_INVENTORY"]["metal_by_karat"]["K21"]["net_grams"] == D("-10.000")


@pytest.mark.asyncio
async def test_order_refunded_line_by_line_to_zero_nets_out_completely(db):
    """Two refund events on one line (1 unit, then the last unit). The header
    ends at zero, which is exactly when a pro-rata-of-remaining would lose the
    discount — the replay prorates against the original totals instead."""
    await _base(db)
    order = _sale("S5", _at(4, 15), qty=2, discount_pct=D("10"))
    db.add(order)
    await db.flush()
    await _refund_line(db, order, _at(5, 3), qty=1)
    await _refund_line(db, order, _at(6, 7), qty=1)
    await db.commit()
    assert order.status == OrderStatus.REFUNDED and order.total_usd == D("0.00")

    await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)

    chain = await _chain(db)
    assert [(e.source_type, e.source_id, e.entry_date) for e in chain] == [
        ("ORDER", "o-S5", date(2026, 4, 15)),
        ("ORDER_REFUND", "it-S5:1", date(2026, 5, 3)),
        ("ORDER_REFUND", "it-S5:2", date(2026, 6, 7)),
    ]
    accts = await _accounts(db)
    for key in ("CASH", "SALES_REVENUE", "SALES_DISCOUNTS", "VAT_PAYABLE", "METAL_COGS"):
        assert accts[key]["net_base"] == D("0.00"), key
    assert accts["METAL_INVENTORY"]["metal_by_karat"]["K21"]["net_grams"] == D("0.000")


@pytest.mark.asyncio
async def test_line_refund_without_a_ledger_event_falls_back_to_the_line_and_says_so(db):
    await _base(db)
    order = _sale("S6", _at(4, 15), qty=2)
    db.add(order)
    await db.flush()
    await _refund_line(db, order, _at(5, 3), qty=1, record_event=False)
    await db.commit()

    report = await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)

    refund = (await _chain(db))[1]
    assert (refund.source_id, refund.entry_date) == ("it-S6:1", date(2026, 5, 3))
    assert any("S6" in w for w in report.warnings)


@pytest.mark.asyncio
async def test_line_refund_events_that_disagree_with_the_line_stop_the_replay(db):
    await _base(db)
    order = _sale("S7", _at(4, 15), qty=3)
    db.add(order)
    await db.flush()
    await _refund_line(db, order, _at(5, 3), qty=1)
    order.items[0].refunded_qty = 2          # the line says 2, the ledger only explains 1
    await db.commit()
    before = await _books(db)

    with pytest.raises(gl_replay.ReplayError, match="S7"):
        await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)
    assert await _books(db) == before


@pytest.mark.asyncio
async def test_void_is_booked_on_the_day_it_happened_and_only_once(db):
    await _base(db)
    db.add(_sale("S2", _at(3, 12), status=OrderStatus.VOIDED, voided_at=_at(4, 2)))
    await db.commit()

    await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)
    again = await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)

    sale, reversal = await _chain(db)
    assert reversal.reverses_entry_id == sale.id
    assert (sale.entry_date, reversal.entry_date) == (date(2026, 3, 12), date(2026, 4, 2))
    assert again.entries_posted == 0
    assert again.kinds[gl_replay.KIND_SALE_REVERSAL].already_posted == 1
    accts = await _accounts(db)
    assert accts["CASH"]["net_base"] == D("0.00")
    assert accts["METAL_INVENTORY"]["metal_by_karat"]["K21"]["net_grams"] == D("0.000")


@pytest.mark.asyncio
async def test_undated_whole_order_refund_is_reversed_on_the_sale_date_with_a_warning(db):
    """POST /orders/{id}/refund only flips the status — no timestamp, no ledger
    event. The replay cannot know the day, so it says so instead of guessing."""
    await _base(db)
    db.add(_sale("S4", _at(6, 1), status=OrderStatus.REFUNDED))
    await db.commit()

    report = await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)

    sale, reversal = await _chain(db)
    assert reversal.reverses_entry_id == sale.id and reversal.entry_date == date(2026, 6, 1)
    assert any("S4" in w and "date" in w for w in report.warnings)
    assert "S4" in gl_replay.format_report(report)


# ── other document types ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_melt_that_changed_nothing_is_counted_but_not_posted(db):
    """Same karat, same weight = the live path posts nothing; so does the replay."""
    await _base(db)
    db.add(Product(id="p2", code="P-2", name_en="Bar", category="Bars", karat=Karat.K21,
                   weight_grams=D("8.000"), margin_percent=D("0"), making_charge=D("0"),
                   status=ProductStatus.MELTED, on_hand_qty=1))
    db.add(GoldLot(id="lot-same", karat=Karat.K21, weight_grams=D("8.000"),
                   weight_remaining_grams=D("8.000"), source=LotSource.MELT,
                   source_ref_type="product", source_ref_id="p2",
                   cost_basis_usd=D("0"), acquired_at=_at(5, 10)))
    await db.commit()

    report = await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)

    melt = report.kinds[gl_replay.KIND_MELT]
    assert (melt.documents, melt.posted, melt.nothing_to_post) == (1, 0, 1)
    assert await _chain(db) == []


@pytest.mark.asyncio
async def test_credit_sale_debits_receivables_and_links_its_invoice(db):
    """The live path stores the sale's GL entry on the AR invoice; the replay
    fills the same link for invoices raised while the flag was off."""
    await _base(db)
    db.add(Customer(id="cust1", name="Layla"))
    db.add(_sale("C1", _at(3, 10), payment=PaymentMethod.CREDIT, customer_id="cust1"))
    db.add(ARInvoice(id="inv1", invoice_no="AR-20260310-001", customer_id="cust1",
                     order_id="o-C1", invoice_date=date(2026, 3, 10), subtotal=D("700"),
                     vat_amount=D("77"), total=D("777"), status=ARInvoiceStatus.OPEN))
    await db.commit()

    await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)

    (sale,) = await _chain(db)
    assert (await db.execute(select(ARInvoice.gl_entry_id))).scalar_one() == sale.id
    accts = await _accounts(db)
    assert accts["AR"]["net_base"] == D("777.00")
    assert "CASH" not in accts


@pytest.mark.asyncio
async def test_documents_with_no_posting_mapper_are_counted_not_guessed(db):
    """AR receipts, vendor bills and stock adjustments other than gold-lot losses
    have no replayable mapper. The report must say how many it left out."""
    await _base(db)
    db.add(Customer(id="cust1", name="Layla"))
    db.add(ARReceipt(id="rc1", receipt_no="RC-20260401-001", customer_id="cust1",
                     receipt_date=date(2026, 4, 1), amount=D("50")))
    db.add(GoldLot(id="lot1", karat=Karat.K21, weight_grams=D("10.000"),
                   weight_remaining_grams=D("12.000"), source=LotSource.SEED,
                   cost_basis_usd=D("600"), acquired_at=_at(3, 1)))
    db.add(ManualAdjustment(id="adj-gain", target_type=AdjustmentTarget.LOT, target_id="lot1",
                            delta=D("2.000"), reason=AdjustmentReason.CORRECTION,
                            notes="recount", occurred_at=_at(3, 2), actor_user_id=ADMIN))
    await db.commit()

    report = await gl_replay.run_replay(db, actor_user_id=ADMIN)

    assert report.entries_posted == 0
    assert report.not_replayed["AR receipts"] == 1
    assert report.not_replayed["Stock adjustments other than gold-lot losses"] == 1
    text = gl_replay.format_report(report)
    assert "AR receipts" in text and "NOT replayed" in text


# ── preconditions ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_replay_refuses_to_run_without_a_chart_of_accounts(db):
    await _base(db, seed_coa=False)
    await _history(db)
    before = await _books(db)

    with pytest.raises(gl_replay.ReplayError, match="[Cc]hart of accounts"):
        await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)

    assert await _books(db) == before
    assert (await db.execute(select(func.count()).select_from(GLAccount))).scalar_one() == 0


@pytest.mark.asyncio
async def test_replay_refuses_a_chart_with_a_missing_or_inactive_system_account(db):
    """Same readiness check as the settings switch (coa_seed.unusable_system_accounts):
    a chart that is only partly there is refused up front, naming each account —
    not discovered halfway through, on whichever document needs it first."""
    await _base(db)
    await _history(db)
    await db.execute(delete(GLAccount).where(GLAccount.system_key == "ADJUSTMENT_EXPENSE"))
    clearing = (await db.execute(
        select(GLAccount).where(GLAccount.system_key == "METAL_CLEARING"))).scalar_one()
    clearing.is_active = False
    await db.commit()
    before = await _books(db)

    with pytest.raises(gl_replay.ReplayError) as exc:
        await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)

    message = str(exc.value)
    assert "1 missing (ADJUSTMENT_EXPENSE)" in message and "1 inactive (METAL_CLEARING)" in message
    assert "adjustment adj1" not in message      # stopped before any document was touched
    assert await _books(db) == before


@pytest.mark.asyncio
async def test_replay_refuses_to_append_to_a_chain_that_does_not_verify(db):
    await _base(db)
    await _history(db)
    head = (await db.execute(select(GLJournalChainHead))).scalar_one()
    head.row_count = 4                       # head claims entries the journal does not have
    await db.commit()
    before = await _books(db)

    # Stopped up front ("refusing to append"), not discovered after posting.
    with pytest.raises(gl_replay.ReplayError, match="hash chain.*[Rr]efusing to append"):
        await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)

    assert await _books(db) == before


@pytest.mark.asyncio
async def test_replay_refuses_an_unknown_actor(db):
    await _base(db)
    await _history(db)

    with pytest.raises(gl_replay.ReplayError, match="actor"):
        await gl_replay.run_replay(db, actor_user_id="nobody", execute=True)

    assert (await _books(db))["entries"] == 0


# ── CLI ───────────────────────────────────────────────────────────────────────

def _session_factory(db):
    """Stand-in for app.db.session.async_session_factory that hands the CLI the
    in-memory test session."""
    @asynccontextmanager
    async def _cm():
        yield db
    return _cm


def test_cli_defaults_to_dry_run_and_needs_execute_to_write():
    from scripts import replay_gl_history as cli

    assert cli.parse_args(["--actor-email", "owner@x.com"]).execute is False
    assert cli.parse_args(["--actor-email", "owner@x.com", "--dry-run"]).execute is False
    assert cli.parse_args(["--actor-email", "owner@x.com", "--execute"]).execute is True
    with pytest.raises(SystemExit):          # the two modes cannot be combined
        cli.parse_args(["--actor-email", "owner@x.com", "--dry-run", "--execute"])
    with pytest.raises(SystemExit):          # every entry needs a named operator
        cli.parse_args([])


@pytest.mark.asyncio
async def test_cli_dry_run_prints_the_report_and_writes_nothing(db, capsys):
    from scripts import replay_gl_history as cli
    await _base(db)
    await _history(db)
    before = await _books(db)

    code = await cli.main(["--actor-email", "owner@x.com"], session_factory=_session_factory(db))

    out = capsys.readouterr().out
    assert code == 0
    assert "DRY RUN" in out and "2026-03" in out and "Sales" in out
    assert "--execute" in out                 # tells the operator how to do it for real
    assert await _books(db) == before


@pytest.mark.asyncio
async def test_cli_execute_writes_and_a_rerun_reports_nothing_to_do(db, capsys):
    from scripts import replay_gl_history as cli
    await _base(db)
    await _history(db)

    code = await cli.main(["--actor-email", "owner@x.com", "--execute"],
                          session_factory=_session_factory(db))
    assert code == 0 and "EXECUTED" in capsys.readouterr().out
    assert (await _books(db))["entries"] == 13

    code = await cli.main(["--actor-email", "owner@x.com", "--execute"],
                          session_factory=_session_factory(db))
    out = capsys.readouterr().out
    assert code == 0
    assert "nothing to post" in out.lower() and "entries committed" not in out.lower()
    assert (await _books(db))["entries"] == 13


@pytest.mark.asyncio
@pytest.mark.parametrize("email", ["till@x.com", "nobody@x.com"])
async def test_cli_refuses_an_actor_who_is_not_an_active_admin(db, capsys, email):
    from scripts import replay_gl_history as cli
    await _base(db)
    await _history(db)

    code = await cli.main(["--actor-email", email, "--execute"],
                          session_factory=_session_factory(db))

    assert code == 2
    assert "admin" in capsys.readouterr().err.lower()
    assert (await _books(db))["entries"] == 0


@pytest.mark.asyncio
async def test_cli_reports_a_failed_replay_and_exits_non_zero(db, capsys):
    from scripts import replay_gl_history as cli
    await _base(db)
    await _history(db)
    db.add(GLPeriod(year=2026, period_no=5, status=PeriodStatus.CLOSED))
    await db.commit()

    code = await cli.main(["--actor-email", "owner@x.com", "--execute"],
                          session_factory=_session_factory(db))

    err = capsys.readouterr().err
    assert code == 1
    assert "nothing was written" in err.lower() and "CLOSED" in err
    assert (await _books(db))["entries"] == 0


# ── end to end, through the real endpoints ────────────────────────────────────

@pytest.mark.asyncio
async def test_history_made_by_the_real_endpoints_replays_then_live_posting_takes_over(db):
    """Trade with the flag OFF using the real handlers (so every row and ledger
    payload has its production shape), replay, switch the flag ON, trade again,
    replay again. The books must be whole and nothing may be posted twice."""
    from app.api.adjustments import create_adjustment
    from app.api.buybacks import create_buyback
    from app.api.melts import create_melt
    from app.api.orders import create_order, refund_order_item, void_order
    from app.api.suppliers import create_payment, create_purchase
    from app.schemas.adjustment import AdjustmentCreate
    from app.schemas.buyback import BuybackCreate
    from app.schemas.order import CheckoutRequest, ItemRefundRequest, OrderItemIn, VoidRequest
    from app.schemas.supplier import PaymentCreate, PurchaseCreate, PurchaseItemIn
    from app.schemas.transitions import MeltCreate

    await _base(db)
    admin = (await db.execute(select(User).where(User.id == ADMIN))).scalar_one()
    db.add_all([
        GoldRateHistory(rate_24k=D("60"), source="test"),
        CoinType(id="coin1", code="C-21", name_en="Coin", karat=Karat.K21,
                 weight_grams=D("10"), margin_mode=MarginMode.USD, margin_value=D("5"),
                 on_hand_qty=20),
        Product(id="ring1", code="R-1", name_en="Ring", category="Rings", karat=Karat.K21,
                weight_grams=D("5.000"), margin_percent=D("10"), making_charge=D("0"),
                on_hand_qty=1),
        Supplier(id="sup1", name="ACME"),
    ])
    await db.commit()

    def checkout(qty):
        return CheckoutRequest(payment_method="CASH", items=[
            OrderItemIn(item_kind="COIN", coin_type_id="coin1", quantity=qty)])

    kept = await create_order(checkout(2), db=db, user=admin)
    voided = await create_order(checkout(1), db=db, user=admin)
    await void_order(voided.id, VoidRequest(reason="wrong customer"), db=db, user=admin)
    returned = await create_order(checkout(3), db=db, user=admin)
    line = returned.items[0].id
    await refund_order_item(returned.id, line, ItemRefundRequest(quantity=1), db=db, user=admin)
    await refund_order_item(returned.id, line, ItemRefundRequest(quantity=2), db=db, user=admin)
    buyback = await create_buyback(BuybackCreate(
        seller_name="Seller", seller_phone="1", kind="PURE_GOLD", karat="K21",
        weight_grams=D("10"), manual_price=D("500")), db=db, user=admin)
    await create_purchase("sup1", PurchaseCreate(
        payment_mode="MIXED", total_cash_due=D("1000"), cash_paid_at_creation=D("300"),
        total_grams_due_by_karat={"K21": D("50")},
        items=[PurchaseItemIn(item_kind="PURE_GOLD", unit_cost_usd=D("3000"),
                              weight_grams=D("50"), karat="K21")]), db=db, user=admin)
    await create_payment("sup1", PaymentCreate(unit="CASH", amount=D("200")), db=db, user=admin)
    await create_adjustment(AdjustmentCreate(
        target_type="LOT", target_id=buyback.result_lot_id, delta=D("-2"), reason="LOSS",
        notes="lost"), db=db, user=admin)
    await create_melt(MeltCreate(product_id="ring1", override_karat="K24",
                                 override_weight_grams=D("4.200")), db=db, user=admin)
    assert (await _books(db))["entries"] == 0          # flag was off throughout

    first = await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)

    assert {k: v.posted for k, v in first.kinds.items()} == {
        gl_replay.KIND_SALE: 3, gl_replay.KIND_SALE_REVERSAL: 1, gl_replay.KIND_LINE_REFUND: 2,
        gl_replay.KIND_SUPPLIER_PURCHASE: 1, gl_replay.KIND_SUPPLIER_PAYMENT: 1,
        gl_replay.KIND_BUYBACK: 1, gl_replay.KIND_MELT: 1, gl_replay.KIND_ADJUSTMENT: 1,
    }
    assert first.warnings == []
    refund_ids = {e.source_id for e in await _chain(db) if e.source_type == "ORDER_REFUND"}
    assert refund_ids == {f"{line}:1", f"{line}:3"}    # the live path's own source ids
    accts = await _accounts(db)
    # Only the kept sale left money in the till: + its total − buyback − purchase − payment.
    assert accts["CASH"]["net_base"] == D(str(kept.total_usd)) - D("500") - D("300") - D("200")
    check = await _verify(db)
    assert check["status"] == "intact" and check["head_matches"] is True

    # Owner signs off → the flag goes on → the next sale posts itself, live.
    cfg = (await db.execute(select(Settings).where(Settings.id == "singleton"))).scalar_one()
    cfg.accounting_auto_post_enabled = True
    await db.commit()
    await create_order(checkout(1), db=db, user=admin)
    after_live_sale = await _books(db)
    assert after_live_sale["entries"] == first.entries_posted + 1

    second = await gl_replay.run_replay(db, actor_user_id=ADMIN, execute=True)

    assert second.entries_posted == 0
    assert second.kinds[gl_replay.KIND_SALE].already_posted == 4   # incl. the live one
    assert await _books(db) == after_live_sale
    check = await _verify(db)
    assert check["status"] == "intact" and check["head_matches"] is True
    await _accounts(db)                                # still balanced in money and metal
