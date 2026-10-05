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

Before anything is kept it refuses, naming what is wrong: a chart of accounts
with a missing or inactive system account, a hash chain that does not verify,
books that were started from an OPENING entry (replaying on top double counts),
and any document dated in a fiscal year that has been year-closed. A step that
fails is reported with its document and date.

Historical values only: amounts come from the documents and from what the
inventory ledger recorded at the time. Where no such record exists and a mapper
can only read today's master data, the document is listed in the report
(ReplayReport.master_data) rather than posted silently.

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

from app.core import gl, gl_postings, ledger, period_close
from app.core.audit_chain import GENESIS_HASH
from app.core.coa_seed import describe_unusable_accounts, unusable_system_accounts
from app.models import (
    AdjustmentTarget, ARInvoice, ARReceipt, GLJournalChainHead, GLJournalEntry,
    GLJournalLine, GLPeriod, GoldLot, InventoryLedger, Karat, LotSource, ManualAdjustment,
    Order, OrderItemKind, OrderStatus, PaymentMethod, Product, Settings, SupplierItemKind,
    SupplierPayment, SupplierPurchase, SupplierPurchaseItem, User, VendorBill, VendorPayment,
    WalkinBuyback,
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
    # Documents posted by this run from a value that has NO historical record,
    # so the mapper read today's master data (e.g. a product's stone cost).
    master_data: list[str] = field(default_factory=list)
    trial_balance: dict = field(default_factory=dict)         # gl.compute_trial_balance, post-replay
    journal_entries: int = 0                                  # GL entries after the replay (chain verified)


@dataclass
class _Step:
    """One source document (or one refund event) to hand to a mapper."""
    when: datetime
    kind: str
    ref: str                                         # human label for error messages
    posted: Callable[[], Awaitable[bool]]            # is its GL entry already there?
    post: Callable[[], Awaitable[GLJournalEntry | None]]
    after: Callable[[GLJournalEntry, list[GLJournalLine]], Awaitable[None]] | None = None
    master_data: str | None = None                   # set when a posted value is not historical

    @property
    def booked_on(self) -> date:
        """The entry date the mapper will use: the document timestamp's own date."""
        return self.when.date()

    def failure(self, exc: Exception) -> "ReplayError":
        """Any error from this step, restated with the document it came from."""
        if isinstance(exc, HTTPException):   # the mappers speak HTTP (422 CLOSED period, …)
            reason = str(exc.detail)
        elif isinstance(exc, ReplayError):
            reason = str(exc)
        else:
            reason = f"{type(exc).__name__}: {exc}"
        return ReplayError(f"{self.ref} ({self.booked_on}): {reason}")


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

async def _tracked_costs(db: AsyncSession, order: Order) -> dict[str, tuple[str, Decimal]]:
    """{order line id: (product code, the product's CURRENT cost_basis_usd)} for
    the lines whose metal COGS the mapper takes from the Product row rather than
    from the line itself (gl_postings._cogs_cost_for_item: a PRODUCT line whose
    product carries a tracked cost). Every other line is costed from its own
    stored gold_rate_at_sale × weight — history by construction."""
    tracked: dict[str, tuple[str, Decimal]] = {}
    for it in order.items:
        if it.item_kind == OrderItemKind.PRODUCT and it.product_id:
            row = (await db.execute(
                select(Product.code, Product.cost_basis_usd).where(Product.id == it.product_id)
            )).first()
            if row is not None and row.cost_basis_usd is not None:
                tracked[it.id] = (row.code, row.cost_basis_usd)
    return tracked


def _no_snapshot_notice(codes: list[str]) -> str:
    return (f"metal COGS for {', '.join(sorted(set(codes)))} is the product's CURRENT cost — "
            "the order line stored no cost snapshot.")


def _sale_step(db: AsyncSession, order: Order, sold, actor_user_id: str,
               tracked: dict[str, tuple[str, Decimal]]) -> _Step:
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
                    f"the metal cost stored on the order is {stored} but the posting mapper "
                    f"computes {computed}. Refusing to post a COGS that differs from what "
                    f"the sale recorded."
                )
        # Credit sale: the live path stores the sale's entry on the AR invoice.
        if order.payment_method == PaymentMethod.CREDIT:
            invoices = (await db.execute(
                select(ARInvoice).where(ARInvoice.order_id == order.id,
                                        ARInvoice.gl_entry_id.is_(None))
            )).scalars().all()
            for inv in invoices:
                inv.gl_entry_id = entry.id

    unrecorded = [tracked[it.id][0] for it in order.items
                  if it.id in tracked and it.cost_basis_usd is None]
    return _Step(
        when=order.created_at, kind=KIND_SALE, ref=f"order {order.order_number}",
        posted=_already(db, gl_postings.SOURCE_ORDER, order.id),
        post=_mapped(gl_postings.post_sale, db, sold, actor_user_id), after=after,
        master_data=_no_snapshot_notice(unrecorded) if unrecorded else None,
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
                       actor_user_id: str, warnings: list[str],
                       tracked: dict[str, tuple[str, Decimal]]) -> list[_Step]:
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

        async def after(entry: GLJournalEntry, lines: list[GLJournalLine]) -> None:
            # The refund mapper re-reads the product's tracked cost. Where the
            # sale stored its cost, the units refunded must reverse exactly
            # their share of THAT — not whatever the product costs today.
            stored = (item.cost_basis_usd * qty / item.quantity).quantize(_Q_MONEY)
            cogs_id = await gl_postings.resolve_account_id(db, "METAL_COGS")
            computed = sum((ln.base_credit for ln in lines if ln.account_id == cogs_id), ZERO)
            if computed != stored:
                raise ReplayError(
                    f"the sale stored a metal cost of {stored} for the refunded unit(s) but "
                    f"the product's cost now gives {computed}. Refusing to reverse a COGS "
                    f"that differs from what the sale recorded."
                )

        reads_product = item.id in tracked
        has_snapshot = item.cost_basis_usd is not None
        return _Step(
            when=when, kind=KIND_LINE_REFUND,
            ref=f"order {order.order_number} line refund ({qty} × {item.product_code})",
            posted=_already(db, gl_postings.SOURCE_ORDER_REFUND, f"{item.id}:{seq}"),
            post=post,
            after=after if reads_product and has_snapshot else None,
            master_data=(_no_snapshot_notice([tracked[item.id][0]])
                         if reads_product and not has_snapshot else None),
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
        tracked = await _tracked_costs(db, order)
        steps.append(_sale_step(db, order, sold, actor_user_id, tracked))
        # The three outcomes are mutually exclusive (see orders.py guards): a
        # line-refunded order cannot be voided, a voided one cannot be refunded.
        for item in refunded:
            steps.extend(_line_refund_steps(
                db, order, sold, item, refund_events.get(item.id, []), actor_user_id, warnings,
                tracked))
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
            post=_mapped(gl_postings.post_supplier_purchase, db, p, actor_user_id),
            master_data=await _stone_cost_notice(db, p)))

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

    # Melts are not a table: each one left a MELT-sourced lot and a MELT event on
    # the inventory ledger whose payload names that lot.
    melt_events: dict[str, InventoryLedger] = {}
    for ev in (await db.execute(
        select(InventoryLedger).where(InventoryLedger.event_type == ledger.EVENT_MELT)
        .order_by(InventoryLedger.occurred_at, InventoryLedger.id)
    )).scalars().all():
        lot_id = (ev.payload or {}).get("lot_id")
        if lot_id:
            melt_events.setdefault(lot_id, ev)

    for lot in (await db.execute(
        select(GoldLot).where(GoldLot.source == LotSource.MELT)
        .order_by(GoldLot.acquired_at, GoldLot.id)
    )).scalars().all():
        melt, notice = await _melt_as_recorded(db, lot, melt_events.get(lot.id))
        if melt is None:
            warnings.append(
                f"Melt lot {lot.id}: its source ({lot.source_ref_type} {lot.source_ref_id}) is "
                f"missing, so the pre-melt karat/weight is unknown — NOT replayed."
            )
            continue
        steps.append(_Step(
            when=melt.occurred_at, kind=KIND_MELT, ref=f"melt lot {lot.id}",
            posted=_already(db, gl_postings.SOURCE_MELT, lot.id),
            post=_mapped(gl_postings.post_melt, db, melt, actor_user_id),
            master_data=notice))

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


async def _stone_cost_notice(db: AsyncSession, purchase: SupplierPurchase) -> str | None:
    """post_supplier_purchase splits the cash cost between Product and Stone
    Inventory by each product's stone cost — read from the Product row as it is
    NOW. The purchase stored no stone cost, so there is no record to check that
    against; say so for every purchase where the split is actually applied."""
    if not purchase.total_cash_due or purchase.total_cash_due <= 0:
        return None
    rows = (await db.execute(
        select(Product.code, Product.stone_cost_usd)
        .join(SupplierPurchaseItem, SupplierPurchaseItem.product_id == Product.id)
        .where(SupplierPurchaseItem.purchase_id == purchase.id,
               SupplierPurchaseItem.item_kind == SupplierItemKind.PRODUCT)
        .order_by(Product.code)
    )).all()
    stones = [f"{code} ({cost:,.2f})" for code, cost in rows if cost]
    if not stones:
        return None
    return ("the stone / product split of its cash cost uses the CURRENT stone cost of "
            f"{', '.join(stones)} — the purchase stored none.")


async def _melt_as_recorded(db: AsyncSession, lot: GoldLot,
                            ev: InventoryLedger | None) -> tuple[SimpleNamespace | None, str | None]:
    """The melt view post_melt takes, built from what was RECORDED when it
    happened: the MELT ledger event (melts.py) holds the resulting karat, weight
    and cost, and says which of karat / weight the admin overrode. A dimension
    that was not overridden went in exactly as it came out, so the event alone
    settles it — the source row is not consulted.

    Only an overridden dimension has no record of its pre-melt value. That one
    is read from the source row: a buyback (a document nothing can edit) or a
    product, which is catalogue data that may have been edited since — hence
    the notice returned alongside. Returns (None, None) when that row is gone."""
    if ev is None:
        # No event at all (melts.py always writes one): the lot row is all there is.
        to_karat, to_grams, cost, when = lot.karat, lot.weight_grams, lot.cost_basis_usd, lot.acquired_at
        karat_changed = grams_changed = True
    else:
        try:
            to_karat = Karat(ev.payload["karat"])
            to_grams = Decimal(str(ev.payload["weight_grams"]))
            cost = Decimal(str(ev.payload["cost_basis_usd"]))
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise ReplayError(f"melt lot {lot.id}: unreadable MELT ledger event ({exc!r}).") from exc
        when = ev.occurred_at
        karat_changed = bool(ev.payload.get("karat_override"))
        grams_changed = bool(ev.payload.get("weight_override"))

    from_karat, from_grams, notice = to_karat, to_grams, None
    if karat_changed or grams_changed:
        model = {"product": Product, "walkin_buyback": WalkinBuyback}.get(lot.source_ref_type)
        source = None
        if model is not None:
            source = (await db.execute(
                select(model).where(model.id == lot.source_ref_id))).scalar_one_or_none()
        if source is None or source.karat is None or source.weight_grams is None:
            return None, None
        if karat_changed:
            from_karat = source.karat
        if grams_changed:
            from_grams = source.weight_grams
        if ev is None:
            notice = ("no MELT ledger event was recorded; the melt is rebuilt from the lot and "
                      f"the CURRENT {lot.source_ref_type} row.")
        elif model is Product:
            what = ("karat and weight are" if karat_changed and grams_changed
                    else "karat is" if karat_changed else "weight is")
            notice = (f"the pre-melt {what} read from product {source.code}'s CURRENT record "
                      "— the melt event stores only the result.")

    return SimpleNamespace(
        id=lot.id, occurred_at=when, from_karat=from_karat, from_grams=from_grams,
        to_karat=to_karat, to_grams=to_grams, cost_usd=cost,
    ), notice


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

async def _journal_size(db: AsyncSession) -> int:
    return (await db.execute(select(func.count()).select_from(GLJournalEntry))).scalar_one()


async def _chain_verifies(db: AsyncSession) -> bool:
    """The GL hash chain walks clean AND its head row agrees with the journal.
    The walk is period_close's — the one a period close already trusts — not a
    copy. Only the head comparison is added: post_entry chains each new entry
    onto the head row, so a head that disagrees with the journal would turn
    every replayed entry into a break."""
    if not await period_close._gl_chain_intact(db):
        return False
    latest = (await db.execute(
        select(GLJournalEntry.entry_hash)
        .order_by(GLJournalEntry.occurred_at.desc(), GLJournalEntry.id.desc()).limit(1)
    )).scalar_one_or_none()
    head_hash, head_count = (await db.execute(
        select(GLJournalChainHead.latest_entry_hash, GLJournalChainHead.row_count)
        .where(GLJournalChainHead.id == 1)
    )).one()
    return head_hash == (latest or GENESIS_HASH) and head_count == await _journal_size(db)


async def _periods(db: AsyncSession) -> set[tuple[int, int]]:
    return {(y, m) for y, m in (await db.execute(select(GLPeriod.year, GLPeriod.period_no))).all()}


async def _refuse_closed_years(db: AsyncSession, steps: list[_Step]) -> set[int]:
    """Precheck: no document may land in a fiscal year that has been year-closed
    (period_close.close_year — its result is already in retained earnings).
    ensure_period would happily create a brand-new OPEN month inside such a
    year, so this runs before anything is kept and lists EVERY offender.

    A date alone is not enough: a melt that changed nothing, or a purchase
    settled in full on the day, never gets an entry and would look "not yet
    posted" on every run after the close. So each closed-year document the GL
    does not hold is put through its mapper here: it offends if that posts an
    entry, opens a period, or is refused (CLOSED period). When any does, the
    caller's rollback discards whatever this probe wrote. Returns the closed
    years among the documents' dates."""
    closed: set[int] = set()
    for year in sorted({step.booked_on.year for step in steps}):
        if await period_close._year_already_closed(db, year):
            closed.add(year)

    offenders: list[str] = []
    for step in steps:
        if step.booked_on.year not in closed:
            continue
        try:
            if await step.posted():
                continue
            periods = await _periods(db)
            touched = await step.post() is not None or await _periods(db) != periods
        except HTTPException:
            touched = True  # refused by the CLOSED period — it did try to post
        except Exception as exc:
            raise step.failure(exc) from exc
        if touched:
            offenders.append(f"{step.ref} ({step.booked_on})")

    if offenders:
        years = ", ".join(str(y) for y in sorted(closed))
        raise ReplayError(
            f"{len(offenders)} document(s) not yet in the GL are dated in a fiscal year that "
            f"has already been closed ({years}). Posting them would change a closed year, so "
            "nothing was posted:\n  - " + "\n  - ".join(offenders) + "\n"
            "The replay never posts into a closed year. Settle these with the accountant "
            "(for example an adjusting entry in the current year)."
        )
    return closed


# ── Entry points ──────────────────────────────────────────────────────────────

async def replay_history(db: AsyncSession, *, actor_user_id: str) -> ReplayReport:
    """Post every not-yet-posted historical document inside the caller's
    transaction (no commit) and report what was posted. Raises ReplayError — or
    whatever a mapper raises — without cleaning up: the CALLER must roll back."""
    report = ReplayReport()

    if (await db.execute(select(User.id).where(User.id == actor_user_id))).first() is None:
        raise ReplayError(f"Unknown actor user id {actor_user_id!r}: every entry needs an actor.")
    problems = await unusable_system_accounts(db)
    if problems:
        raise ReplayError(
            f"The chart of accounts is not ready: {describe_unusable_accounts(problems)}. "
            "Seed it (POST /api/accounting/seed-coa) and reactivate what is inactive "
            "first — the replay never creates or changes accounts."
        )
    if not await _chain_verifies(db):
        raise ReplayError(
            "The GL hash chain does not verify (GET /api/accounting/ledger/verify). "
            "Refusing to append a backfill to a chain that is already broken."
        )

    opening = (await db.execute(
        select(GLJournalEntry.entry_no, GLJournalEntry.entry_date)
        .where(GLJournalEntry.source_type == gl.SOURCE_OPENING)
        .order_by(GLJournalEntry.entry_date, GLJournalEntry.entry_no)
    )).first()
    if opening is not None:
        raise ReplayError(
            f"These books were started from an opening-balance entry ({opening.entry_no}, "
            f"dated {opening.entry_date}). An opening entry is a snapshot of the stock on hand "
            "and of what suppliers are owed — the net result of the same purchases, sales and "
            "payments this replay would post. Replaying history on top of it would double count "
            "stock and payables, so nothing was posted. Books that start from an opening "
            "snapshot go forward with auto-posting only."
        )

    report.auto_post_enabled = (await db.execute(
        select(Settings.accounting_auto_post_enabled).where(Settings.id == "singleton")
    )).scalar_one_or_none()
    periods_before = await _periods(db)

    steps = await _order_steps(db, actor_user_id, report.warnings)
    steps += await _document_steps(db, actor_user_id, report.warnings)
    # Original chronological order, so entry dates rise along the hash chain.
    steps.sort(key=lambda s: (_utc(s.when), _RANK[s.kind]))
    closed_years = await _refuse_closed_years(db, steps)

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
            if entry.entry_date.year in closed_years:
                # Cannot happen after the precheck unless a step only became
                # postable during this run; either way a closed year stays closed.
                raise ReplayError(f"its entry would be dated in closed fiscal year {entry.entry_date.year}.")
        except Exception as exc:
            # Whatever went wrong, the operator needs to know on WHICH document.
            raise step.failure(exc) from exc
        summary.posted += 1
        if step.master_data:
            report.master_data.append(f"{step.ref} ({step.booked_on}): {step.master_data}")
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
    if any(year in closed_years for year, _ in created):
        raise ReplayError("A period was opened inside a closed fiscal year — nothing kept.")
    report.periods_created = sorted(f"{y}-{m:02d}" for y, m in created)
    report.periods_reused = sorted({_month(d) for d in entry_dates} - set(report.periods_created))
    report.not_replayed = await _not_replayed(db)

    # Never commit books that do not balance or a chain that does not verify.
    report.trial_balance = await gl.compute_trial_balance(db, as_of=date.max)
    if not (report.trial_balance["balanced"] and report.trial_balance["metal_balanced"]):
        raise ReplayError("Trial balance does not balance after the replay — nothing kept.")
    if not await _chain_verifies(db):
        raise ReplayError("The GL hash chain does not verify after the replay — nothing kept.")
    report.journal_entries = await _journal_size(db)

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
    if report.master_data:
        out += ["", "Uses CURRENT master data — no historical record exists for these values, so "
                    "they are as of today. Check each before sign-off:"]
        out += [f"  - {m}" for m in report.master_data]

    tb = report.trial_balance
    out += ["", f"Trial balance after replay: debits {tb['total_base_debit']:,.2f} / credits "
                f"{tb['total_base_credit']:,.2f} — money {'balanced' if tb['balanced'] else 'UNBALANCED'}, "
                f"metal {'balanced' if tb['metal_balanced'] else 'UNBALANCED'}",
            f"Hash chain after replay: verified, {report.journal_entries} entries"]
    if tb["accounts"]:
        out += ["", "Balances after replay (net = debits − credits):"]
        for a in tb["accounts"]:
            metal = ", ".join(f"{k} {v['net_grams']} g" for k, v in sorted(a["metal_by_karat"].items()))
            out.append(f"  {a['code']:<8}{a['name']:<28}{a['net_base']:>15,.2f}" + (f"  {metal}" if metal else ""))
    return "\n".join(out)
