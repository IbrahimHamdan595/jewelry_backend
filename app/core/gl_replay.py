"""Historical GL replay (NEX-52): post what happened BEFORE auto-posting was
switched on, through the same mappers the live paths use, at the original dates.

The auto-posting bridge (app/core/gl_postings.py) shipped behind a settings flag
that defaults to OFF, so the shop traded with the inventory ledger recording
every operation and the general ledger recording none. This module walks those
operations in chronological order and hands each one to its existing `post_*`
mapper. The mappers decide the debits and credits; nothing here re-derives an
amount, and their per-source idempotency is what makes a re-run safe.

Like the mappers, replay_history() runs inside the caller's transaction and
never commits. run_replay() is the operator entry point and owns the
transaction: COMMIT on execute, ROLLBACK on a dry run and on any error. A dry
run is therefore the SAME code path as an execute (it reports exactly what
would be posted), and a failure halfway leaves no entry, no auto-opened period
and no advanced hash chain behind.

The auto-post flag is neither required nor touched: the mappers are handed a
stand-in settings object whose gate is open (see _gate).

Deliberately NOT wired to startup or to any endpoint. Operator tool:
scripts/replay_gl_history.py. Decision record + runbook: docs/GL_HISTORY_REPLAY.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Awaitable, Callable

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import gl, gl_postings, ledger
from app.core.audit_chain import GENESIS_HASH, _GL_HEADER_FIELDS, _GL_LINE_FIELDS, verify_gl_chain
from app.models import (
    AdjustmentTarget, ARInvoice, ARReceipt, GLAccount, GLJournalChainHead, GLJournalEntry,
    GLJournalLine, GLPeriod, GoldLot, InventoryLedger, LotSource, ManualAdjustment, Order,
    OrderStatus, PaymentMethod, Product, Settings, SupplierPayment, SupplierPurchase, User,
    VendorBill, VendorPayment, WalkinBuyback,
)

ZERO = Decimal("0")
_Q_MONEY = Decimal("0.01")

KIND_SALE = "SALE"
KIND_SALE_REVERSAL = "SALE_REVERSAL"
KIND_LINE_REFUND = "LINE_REFUND"
KIND_SUPPLIER_PURCHASE = "SUPPLIER_PURCHASE"
KIND_SUPPLIER_PAYMENT = "SUPPLIER_PAYMENT"
KIND_BUYBACK = "BUYBACK"
KIND_MELT = "MELT"
KIND_ADJUSTMENT = "ADJUSTMENT"

# One row per posting mapper in gl_postings.py (post_order_refund has two shapes:
# a full reversal and a per-line refund). Report order; the position also breaks
# ties between steps stamped at the same instant, so a sale precedes its own void.
KIND_LABELS: dict[str, str] = {
    KIND_SALE: "Sales",
    KIND_SALE_REVERSAL: "Sale voids / whole-order refunds",
    KIND_LINE_REFUND: "Line refunds",
    KIND_SUPPLIER_PURCHASE: "Supplier purchases",
    KIND_SUPPLIER_PAYMENT: "Supplier payments",
    KIND_BUYBACK: "Walk-in buybacks",
    KIND_MELT: "Melts",
    KIND_ADJUSTMENT: "Gold-lot losses",
}
_RANK = {kind: i for i, kind in enumerate(KIND_LABELS)}


class ReplayError(Exception):
    """The replay cannot proceed, or refuses to. Nothing has been committed."""


@dataclass
class KindSummary:
    documents: int = 0          # source documents found
    posted: int = 0             # journal entries posted by this run
    already_posted: int = 0     # skipped — the GL already holds their entry
    nothing_to_post: int = 0    # the mapper has no entry for them (e.g. a melt that changed nothing)
    base_total: Decimal = ZERO  # Σ USD-base debits of the entries posted
    grams_by_karat: dict[str, Decimal] = field(default_factory=dict)  # Σ debit grams per karat


@dataclass
class ReplayReport:
    executed: bool = False
    auto_post_enabled: bool | None = None  # the stored flag, for display only
    kinds: dict[str, KindSummary] = field(
        default_factory=lambda: {kind: KindSummary() for kind in KIND_LABELS})
    entries_posted: int = 0
    first_entry_date: date | None = None
    last_entry_date: date | None = None
    periods_created: list[str] = field(default_factory=list)  # "YYYY-MM", created OPEN
    periods_reused: list[str] = field(default_factory=list)   # existed already, received entries
    not_replayed: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    trial_balance: dict = field(default_factory=dict)         # gl.compute_trial_balance, post-replay
    chain: dict = field(default_factory=dict)                 # status / total_rows / head_matches


@dataclass
class _Step:
    """One source document (or one refund event) to hand to a mapper."""
    when: datetime
    kind: str
    ref: str                                         # human label for error messages
    posted: Callable[[], Awaitable[bool]]            # is its GL entry already there?
    post: Callable[[], Awaitable[GLJournalEntry | None]]
    after: Callable[[GLJournalEntry, list[GLJournalLine]], Awaitable[None]] | None = None


def _utc(dt: datetime) -> datetime:
    """Comparable timestamp. Postgres hands back tz-aware UTC, the SQLite test
    fixture hands back naive — and a naive value is UTC by the same convention
    audit_chain._canonical uses."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _month(d: date) -> str:
    return f"{d.year}-{d.month:02d}"


def _gate(**snapshot) -> SimpleNamespace:
    """Stand-in for the Settings row the mappers take. It opens their auto-post
    gate for this replay only: the stored flag is not consulted and never
    written. A mapper reading any other setting must be given the HISTORICAL
    value explicitly (see vat_percent on line refunds) — anything else raises
    AttributeError and rolls the replay back, rather than silently using today's."""
    return SimpleNamespace(accounting_auto_post_enabled=True, **snapshot)


def _as_sold(order: Order) -> SimpleNamespace:
    """The order as it was SOLD. A line refund overwrites the header (subtotal /
    VAT / discount / totals) with what REMAINS (orders._recompute_order_totals),
    so for those orders the header is rebuilt exactly the way checkout computed
    it: Σ line final_price at the order's own vat_percent and discount_percent.
    Orders without line refunds are passed to the mappers untouched."""
    subtotal = sum((it.final_price for it in order.items), ZERO).quantize(_Q_MONEY)
    vat_amount = (subtotal * order.vat_percent / Decimal(100)).quantize(_Q_MONEY)
    discount_amount = (subtotal * order.discount_percent / Decimal(100)).quantize(_Q_MONEY)
    return SimpleNamespace(
        id=order.id, order_number=order.order_number, created_at=order.created_at,
        payment_method=order.payment_method, items=order.items,
        subtotal=subtotal, vat_amount=vat_amount, discount_amount=discount_amount,
        total_usd=subtotal + vat_amount - discount_amount,
    )


def _mapped(mapper, db: AsyncSession, doc, actor_user_id: str):
    async def post():
        return await mapper(db, doc, _gate(), actor_user_id)
    return post


def _already(db: AsyncSession, source_type: str, source_id: str):
    async def posted() -> bool:
        return await gl_postings.find_live_entry(db, source_type, source_id) is not None
    return posted


# ── Sales, voids, refunds ─────────────────────────────────────────────────────

def _sale_step(db: AsyncSession, order: Order, sold, actor_user_id: str) -> _Step:
    async def after(entry: GLJournalEntry, lines: list[GLJournalLine]) -> None:
        # Historical COGS must be what the order stored at checkout
        # (OrderItem.cost_basis_usd), never a re-valuation. The mapper derives it
        # from the line's gold_rate_at_sale / the product's tracked cost — both
        # fixed at sale time — so the two agree; if they ever do not, stop
        # instead of writing a different history into an immutable ledger.
        if all(it.cost_basis_usd is not None for it in order.items):
            stored = sum((it.cost_basis_usd for it in order.items), ZERO).quantize(_Q_MONEY)
            cogs_id = await gl_postings.resolve_account_id(db, "METAL_COGS")
            computed = sum((ln.base_debit for ln in lines if ln.account_id == cogs_id), ZERO)
            if computed != stored:
                raise ReplayError(
                    f"order {order.order_number}: the metal cost stored on the order is "
                    f"{stored} but the posting mapper computes {computed}. Refusing to post "
                    f"a COGS that differs from what the sale recorded."
                )
        # Credit sale: the live path stores the sale's entry on the AR invoice.
        if order.payment_method == PaymentMethod.CREDIT:
            invoices = (await db.execute(
                select(ARInvoice).where(ARInvoice.order_id == order.id,
                                        ARInvoice.gl_entry_id.is_(None))
            )).scalars().all()
            for inv in invoices:
                inv.gl_entry_id = entry.id

    return _Step(
        when=order.created_at, kind=KIND_SALE, ref=f"order {order.order_number}",
        posted=_already(db, gl_postings.SOURCE_ORDER, order.id),
        post=_mapped(gl_postings.post_sale, db, sold, actor_user_id), after=after,
    )


def _reversal_step(db: AsyncSession, order: Order, actor_user_id: str,
                   warnings: list[str]) -> _Step:
    """VOIDED, or REFUNDED through the whole-order endpoint → full reversal."""
    when = order.voided_at if order.status == OrderStatus.VOIDED else None
    if when is None:
        # POST /orders/{id}/refund only flips the status: no timestamp, no ledger
        # event. The day cannot be recovered, so it is not invented.
        when = order.created_at
        warnings.append(
            f"Order {order.order_number} is {order.status.value} but no void/refund date "
            f"was recorded; its reversal is booked on the sale date {_utc(when).date()}."
        )

    async def posted() -> bool:
        # Not find_live_entry(ORDER_REFUND, order.id): a full reversal is stored
        # as source_type REVERSAL with reverses_entry_id set, which that lookup
        # can never match. Look for the reversal itself.
        original = await gl_postings.find_live_entry(db, gl_postings.SOURCE_ORDER, order.id)
        if original is None:
            return False
        return (await db.execute(
            select(GLJournalEntry.id).where(GLJournalEntry.reverses_entry_id == original.id)
        )).first() is not None

    async def post():
        return await gl_postings.post_order_refund(
            db, order, _gate(), actor_user_id, refunded_item=None, entry_date=when.date())

    return _Step(when=when, kind=KIND_SALE_REVERSAL,
                 ref=f"order {order.order_number} {order.status.value.lower()}",
                 posted=posted, post=post)


def _line_refund_steps(db: AsyncSession, order: Order, sold, item, events: list[InventoryLedger],
                       actor_user_id: str, warnings: list[str]) -> list[_Step]:
    """One step per refund EVENT on the line, read back from the inventory
    ledger (ORDER_ITEM_REFUND), so each lands on its own date under the source
    id the live path uses ({item.id}:{cumulative refunded qty}) — a refund
    already posted live is skipped, never doubled."""
    try:
        slices = [
            (ev.occurred_at, int(ev.payload["refunded_qty"]),
             Decimal(str(ev.payload["refund_amount"])), int(ev.payload["refunded_qty_total"]))
            for ev in events
        ]
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise ReplayError(
            f"order {order.order_number}, line {item.product_code}: unreadable "
            f"ORDER_ITEM_REFUND ledger event ({exc!r})."
        ) from exc

    if not slices:
        # No ledger trail (the refund handler writes one in the same transaction,
        # so this is legacy data). The line's own counters are all there is.
        when = item.refunded_at or order.created_at
        slices = [(when, item.refunded_qty, item.refunded_amount, item.refunded_qty)]
        warnings.append(
            f"Order {order.order_number}, line {item.product_code}: no refund event on the "
            f"inventory ledger; {item.refunded_qty} refunded unit(s) are booked as one entry "
            f"on {_utc(when).date()}."
        )
    elif sum(qty for _, qty, _, _ in slices) != item.refunded_qty:
        raise ReplayError(
            f"order {order.order_number}, line {item.product_code}: the inventory ledger "
            f"explains {sum(qty for _, qty, _, _ in slices)} refunded unit(s) but the line "
            f"records {item.refunded_qty}. Refusing to guess — investigate before replaying."
        )

    def step(when: datetime, qty: int, value: Decimal, seq: int) -> _Step:
        async def post():
            # `sold` (original totals) so the discount is prorated against the sale,
            # and the ORDER's vat_percent, not today's setting.
            return await gl_postings.post_order_refund(
                db, sold, _gate(vat_percent=order.vat_percent), actor_user_id,
                refunded_item=item, refund_value=value, refund_qty=qty, refund_seq=seq,
                entry_date=when.date())

        return _Step(
            when=when, kind=KIND_LINE_REFUND,
            ref=f"order {order.order_number} line refund ({qty} × {item.product_code})",
            posted=_already(db, gl_postings.SOURCE_ORDER_REFUND, f"{item.id}:{seq}"),
            post=post,
        )

    return [step(*s) for s in slices]


async def _order_steps(db: AsyncSession, actor_user_id: str, warnings: list[str]) -> list[_Step]:
    orders = (await db.execute(
        select(Order).options(selectinload(Order.items)).order_by(Order.created_at, Order.id)
    )).scalars().all()
    refund_events: dict[str, list[InventoryLedger]] = {}
    for ev in (await db.execute(
        select(InventoryLedger)
        .where(InventoryLedger.event_type == ledger.EVENT_ORDER_ITEM_REFUND,
               InventoryLedger.ref_type == "order_item")
        .order_by(InventoryLedger.occurred_at, InventoryLedger.id)
    )).scalars().all():
        refund_events.setdefault(ev.ref_id, []).append(ev)

    steps: list[_Step] = []
    for order in orders:
        refunded = [it for it in order.items if it.refunded_qty]
        sold = _as_sold(order) if refunded else order
        steps.append(_sale_step(db, order, sold, actor_user_id))
        # The three outcomes are mutually exclusive (see orders.py guards): a
        # line-refunded order cannot be voided, a voided one cannot be refunded.
        for item in refunded:
            steps.extend(_line_refund_steps(
                db, order, sold, item, refund_events.get(item.id, []), actor_user_id, warnings))
        if not refunded and order.status in (OrderStatus.VOIDED, OrderStatus.REFUNDED):
            steps.append(_reversal_step(db, order, actor_user_id, warnings))
    return steps


# ── Purchases, payments, buybacks, melts, adjustments ─────────────────────────

async def _document_steps(db: AsyncSession, actor_user_id: str, warnings: list[str]) -> list[_Step]:
    steps: list[_Step] = []

    for p in (await db.execute(
        select(SupplierPurchase).order_by(SupplierPurchase.occurred_at, SupplierPurchase.id)
    )).scalars().all():
        steps.append(_Step(
            when=p.occurred_at, kind=KIND_SUPPLIER_PURCHASE, ref=f"supplier purchase {p.id}",
            posted=_already(db, gl_postings.SOURCE_SUPPLIER_PURCHASE, p.id),
            post=_mapped(gl_postings.post_supplier_purchase, db, p, actor_user_id)))

    for pay in (await db.execute(
        select(SupplierPayment).order_by(SupplierPayment.paid_at, SupplierPayment.id)
    )).scalars().all():
        steps.append(_Step(
            when=pay.paid_at, kind=KIND_SUPPLIER_PAYMENT, ref=f"supplier payment {pay.id}",
            posted=_already(db, gl_postings.SOURCE_SUPPLIER_PAYMENT, pay.id),
            post=_mapped(gl_postings.post_supplier_payment, db, pay, actor_user_id)))

    for bb in (await db.execute(
        select(WalkinBuyback).order_by(WalkinBuyback.occurred_at, WalkinBuyback.id)
    )).scalars().all():
        steps.append(_Step(
            when=bb.occurred_at, kind=KIND_BUYBACK, ref=f"buyback {bb.id}",
            posted=_already(db, gl_postings.SOURCE_BUYBACK, bb.id),
            post=_mapped(gl_postings.post_buyback, db, bb, actor_user_id)))

    # Melts are not a table: each one left a MELT-sourced lot pointing back at the
    # product / used-product buyback it consumed. Same view melts.py builds.
    for lot in (await db.execute(
        select(GoldLot).where(GoldLot.source == LotSource.MELT)
        .order_by(GoldLot.acquired_at, GoldLot.id)
    )).scalars().all():
        model = {"product": Product, "walkin_buyback": WalkinBuyback}.get(lot.source_ref_type)
        source = None
        if model is not None:
            source = (await db.execute(
                select(model).where(model.id == lot.source_ref_id))).scalar_one_or_none()
        if source is None or source.karat is None or source.weight_grams is None:
            warnings.append(
                f"Melt lot {lot.id}: its source ({lot.source_ref_type} {lot.source_ref_id}) is "
                f"missing, so the pre-melt karat/weight is unknown — NOT replayed."
            )
            continue
        melt = SimpleNamespace(
            id=lot.id, occurred_at=lot.acquired_at,
            from_karat=source.karat, from_grams=source.weight_grams,
            to_karat=lot.karat, to_grams=lot.weight_grams, cost_usd=lot.cost_basis_usd,
        )
        steps.append(_Step(
            when=lot.acquired_at, kind=KIND_MELT, ref=f"melt lot {lot.id}",
            posted=_already(db, gl_postings.SOURCE_MELT, lot.id),
            post=_mapped(gl_postings.post_melt, db, melt, actor_user_id)))

    # Only a gold-lot LOSS posts (adjustments.py): gains and product / coin /
    # ounce stock adjustments have no mapper, live or replayed.
    for adj in (await db.execute(
        select(ManualAdjustment).where(ManualAdjustment.target_type == AdjustmentTarget.LOT,
                                       ManualAdjustment.delta < 0)
        .order_by(ManualAdjustment.occurred_at, ManualAdjustment.id)
    )).scalars().all():
        lot = (await db.execute(
            select(GoldLot).where(GoldLot.id == adj.target_id))).scalar_one_or_none()
        if lot is None:
            warnings.append(f"Adjustment {adj.id}: lot {adj.target_id} is missing — NOT replayed.")
            continue
        lost = -adj.delta
        cost = (
            (lot.cost_basis_usd * lost / lot.weight_grams)
            if lot.weight_grams and lot.weight_grams > 0 else ZERO
        )
        loss = SimpleNamespace(id=adj.id, occurred_at=adj.occurred_at, karat=lot.karat,
                               grams=lost, cost_usd=cost)
        steps.append(_Step(
            when=adj.occurred_at, kind=KIND_ADJUSTMENT, ref=f"adjustment {adj.id}",
            posted=_already(db, gl_postings.SOURCE_ADJUSTMENT, adj.id),
            post=_mapped(gl_postings.post_adjustment, db, loss, actor_user_id)))

    return steps


async def _not_replayed(db: AsyncSession) -> dict[str, int]:
    """Activity this tool leaves out, counted so the operator is not surprised.
    AR and expense documents post from inside their create-and-post functions
    (app/core/ar.py, app/core/expenses.py) — there is no idempotent mapper to
    re-run. The adjustment kinds below are not posted by the live path either."""
    async def count(model, *where) -> int:
        return (await db.execute(
            select(func.count()).select_from(model).where(*where))).scalar_one()

    counts = {
        "AR invoices not tied to a sale": await count(
            ARInvoice, ARInvoice.order_id.is_(None), ARInvoice.gl_entry_id.is_(None)),
        "AR receipts": await count(ARReceipt, ARReceipt.gl_entry_id.is_(None)),
        "Vendor bills": await count(VendorBill, VendorBill.gl_entry_id.is_(None)),
        "Vendor payments": await count(VendorPayment, VendorPayment.gl_entry_id.is_(None)),
        "Stock adjustments other than gold-lot losses": await count(
            ManualAdjustment,
            or_(ManualAdjustment.target_type != AdjustmentTarget.LOT, ManualAdjustment.delta >= 0)),
    }
    return {label: n for label, n in counts.items() if n}


# ── Chain + periods ───────────────────────────────────────────────────────────

async def _verify_chain(db: AsyncSession) -> dict:
    """The walk GET /accounting/ledger/verify does, plus its head comparison."""
    entries = (await db.execute(
        select(GLJournalEntry).options(selectinload(GLJournalEntry.lines))
        .order_by(GLJournalEntry.occurred_at, GLJournalEntry.id)
    )).scalars().all()
    result = verify_gl_chain(
        {"id": e.id, "prev_hash": e.prev_hash, "entry_hash": e.entry_hash,
         **{f: getattr(e, f) for f in _GL_HEADER_FIELDS},
         "lines": [{f: getattr(ln, f) for f in _GL_LINE_FIELDS} for ln in e.lines]}
        for e in entries
    )
    head = (await db.execute(
        select(GLJournalChainHead).where(GLJournalChainHead.id == 1))).scalar_one()
    computed = entries[-1].entry_hash if entries else GENESIS_HASH
    return {
        "status": result["status"], "total_rows": result["total_rows"],
        "head_matches": head.latest_entry_hash == computed and head.row_count == len(entries),
    }


def _chain_ok(chain: dict) -> bool:
    return chain["status"] in ("intact", "empty") and chain["head_matches"]


async def _periods(db: AsyncSession) -> set[tuple[int, int]]:
    return {(y, m) for y, m in (await db.execute(select(GLPeriod.year, GLPeriod.period_no))).all()}


# ── Entry points ──────────────────────────────────────────────────────────────

async def replay_history(db: AsyncSession, *, actor_user_id: str) -> ReplayReport:
    """Post every not-yet-posted historical document inside the caller's
    transaction (no commit) and report what was posted. Raises ReplayError — or
    whatever a mapper raises — without cleaning up: the CALLER must roll back."""
    report = ReplayReport()

    if (await db.execute(select(User.id).where(User.id == actor_user_id))).first() is None:
        raise ReplayError(f"Unknown actor user id {actor_user_id!r}: every entry needs an actor.")
    if not (await db.execute(
        select(func.count()).select_from(GLAccount).where(GLAccount.system_key.is_not(None))
    )).scalar_one():
        raise ReplayError(
            "Chart of accounts is not seeded. Seed it first (POST /api/accounting/seed-coa) "
            "— the replay never creates accounts."
        )
    if not _chain_ok(await _verify_chain(db)):
        raise ReplayError(
            "The GL hash chain does not verify (GET /api/accounting/ledger/verify). "
            "Refusing to append a backfill to a chain that is already broken."
        )

    report.auto_post_enabled = (await db.execute(
        select(Settings.accounting_auto_post_enabled).where(Settings.id == "singleton")
    )).scalar_one_or_none()
    periods_before = await _periods(db)

    steps = await _order_steps(db, actor_user_id, report.warnings)
    steps += await _document_steps(db, actor_user_id, report.warnings)
    # Original chronological order, so entry dates rise along the hash chain.
    steps.sort(key=lambda s: (_utc(s.when), _RANK[s.kind]))

    entry_dates: list[date] = []
    for step in steps:
        summary = report.kinds[step.kind]
        summary.documents += 1
        try:
            if await step.posted():
                summary.already_posted += 1
                continue
            entry = await step.post()
            if entry is None:
                summary.nothing_to_post += 1
                continue
            lines = list((await db.execute(
                select(GLJournalLine).where(GLJournalLine.entry_id == entry.id)
            )).scalars().all())
            if step.after is not None:
                await step.after(entry, lines)
        except HTTPException as exc:
            # The mappers speak HTTP (422 for a CLOSED period, an unseeded
            # account…). Tell the operator which document hit it.
            raise ReplayError(f"{step.ref} ({_utc(step.when).date()}): {exc.detail}") from exc
        summary.posted += 1
        summary.base_total += sum((ln.base_debit for ln in lines), ZERO)
        for ln in lines:
            if ln.metal_debit_grams:
                summary.grams_by_karat[ln.karat] = (
                    summary.grams_by_karat.get(ln.karat, ZERO) + ln.metal_debit_grams)
        entry_dates.append(entry.entry_date)

    report.entries_posted = len(entry_dates)
    if entry_dates:
        report.first_entry_date, report.last_entry_date = min(entry_dates), max(entry_dates)
    created = await _periods(db) - periods_before
    report.periods_created = sorted(f"{y}-{m:02d}" for y, m in created)
    report.periods_reused = sorted({_month(d) for d in entry_dates} - set(report.periods_created))
    report.not_replayed = await _not_replayed(db)

    # Never commit books that do not balance or a chain that does not verify.
    report.trial_balance = await gl.compute_trial_balance(db, as_of=date.max)
    if not (report.trial_balance["balanced"] and report.trial_balance["metal_balanced"]):
        raise ReplayError("Trial balance does not balance after the replay — nothing kept.")
    report.chain = await _verify_chain(db)
    if not _chain_ok(report.chain):
        raise ReplayError("The GL hash chain does not verify after the replay — nothing kept.")

    if report.entries_posted:
        # One marker on the audit ledger, so entries whose entry_date is months
        # before their occurred_at are explainable. Absent on a no-op re-run.
        await ledger.record(
            db, event_type=ledger.EVENT_GL_HISTORY_REPLAYED, actor_user_id=actor_user_id,
            ref_type="gl_journal", ref_id="history_replay",
            payload={
                "entries_posted": report.entries_posted,
                "by_kind": {k: s.posted for k, s in report.kinds.items() if s.posted},
                "first_entry_date": report.first_entry_date.isoformat(),
                "last_entry_date": report.last_entry_date.isoformat(),
                "periods_created": report.periods_created,
            },
        )
    return report


async def run_replay(db: AsyncSession, *, actor_user_id: str, execute: bool = False) -> ReplayReport:
    """Operator entry point: the whole replay as ONE transaction. Commits only
    when `execute` is true; a dry run (the default) and any failure roll back.
    Call it on a session with no pending work — it ends the session's
    transaction either way."""
    try:
        report = await replay_history(db, actor_user_id=actor_user_id)
    except Exception:
        await db.rollback()
        raise
    if execute:
        await db.commit()
    else:
        await db.rollback()
    report.executed = execute
    return report


def format_report(report: ReplayReport) -> str:
    """Plain-text report for the operator (and for the owner's sign-off)."""
    done = report.executed
    if not done:
        mode = "DRY RUN (nothing was written)"
    elif report.entries_posted:
        mode = "EXECUTED (entries committed)"
    else:
        mode = "EXECUTED (nothing to post — every document is already in the GL)"
    out = [
        "GL history replay — " + mode,
        "Auto-post flag: " + {True: "ON", False: "OFF", None: "no settings row"}[report.auto_post_enabled]
        + " — the replay neither needs it nor changes it.",
        "",
        f"{'Document type':<34}{'found':>6}{'posted' if done else 'to post':>9}"
        f"{'already':>9}{'nothing':>9}{'USD (Σ debits)':>17}  grams (Σ debits)",
    ]
    for kind, label in KIND_LABELS.items():
        s = report.kinds[kind]
        grams = ", ".join(f"{k} {v}" for k, v in sorted(s.grams_by_karat.items())) or "-"
        out.append(f"{label:<34}{s.documents:>6}{s.posted:>9}{s.already_posted:>9}"
                   f"{s.nothing_to_post:>9}{s.base_total:>17,.2f}  {grams}")
    span = (f" (entry dates {report.first_entry_date} → {report.last_entry_date})"
            if report.entries_posted else "")
    out += ["", f"Journal entries {'posted' if done else 'that would be posted'}: "
                f"{report.entries_posted}{span}"]

    out += ["", f"Accounting periods {'created' if done else 'that would be created'} "
                "(as OPEN — review and close them afterwards):",
            "  " + (", ".join(report.periods_created) or "none"),
            "Existing periods that " + ("received" if done else "would receive") + " entries: "
            + (", ".join(report.periods_reused) or "none")]

    if report.not_replayed:
        out += ["", "NOT replayed — no posting mapper exists for these; settle them with the accountant:"]
        out += [f"  {label}: {n}" for label, n in report.not_replayed.items()]
    if report.warnings:
        out += ["", "Warnings:"] + [f"  - {w}" for w in report.warnings]

    tb = report.trial_balance
    out += ["", f"Trial balance after replay: debits {tb['total_base_debit']:,.2f} / credits "
                f"{tb['total_base_credit']:,.2f} — money {'balanced' if tb['balanced'] else 'UNBALANCED'}, "
                f"metal {'balanced' if tb['metal_balanced'] else 'UNBALANCED'}",
            f"Hash chain after replay: {report.chain['status']}, {report.chain['total_rows']} entries"]
    if tb["accounts"]:
        out += ["", "Balances after replay (net = debits − credits):"]
        for a in tb["accounts"]:
            metal = ", ".join(f"{k} {v['net_grams']} g" for k, v in sorted(a["metal_by_karat"].items()))
            out.append(f"  {a['code']:<8}{a['name']:<28}{a['net_base']:>15,.2f}" + (f"  {metal}" if metal else ""))
    return "\n".join(out)
