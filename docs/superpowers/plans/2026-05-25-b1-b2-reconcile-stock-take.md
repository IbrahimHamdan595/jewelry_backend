# B1 + B2 — Coin/Ounce Reconciliation + Physical Stock-Take

> **Status:** awaiting approval. NO code written yet.

**Goal:** prove that the system's recorded inventory matches both (a) the
ledger's event history (reconcile) and (b) the physical reality of what's
in the safe (stock-take). Make every adjustment auditable through the
existing append-only chain — a stock-take must never silently overwrite
`on_hand_qty`.

**Architecture principle (your explicit ask):**
> "accepting a variance must post a fully-audited adjustment through the
> existing ledger — a stock-take must never silently overwrite on_hand_qty,
> or it becomes a backdoor around everything we just built."

This drives the central design decision: the stock-take module is a
**proposer**, not a writer. The only path that mutates `on_hand_qty` is
`MANUAL_ADJUSTMENT` via the existing `adjustments` router (which already
writes to the ledger via `record()` and is now hash-chained + DB-trigger
protected by A1/A2). Approving a stock-take variance creates a
`ManualAdjustment` row using that path. No shortcut, no bypass.

---

## Split into two phases (independently reviewable)

**B1 — Coin/ounce stock reconciliation.** Read-only. Replays the source
tables (purchases, buybacks, sales, adjustments, melts) against
`on_hand_qty` and reports drift. No UI write surface; one button in the
existing Inventory tab that calls the endpoint. Small, mostly mirrors the
existing supplier-balance `/reconcile` shape.

**B2 — Physical stock-take with variance workflow.** Bigger. New tables,
state machine, two-stage admin UI (count + approval), and the careful
plumbing of variance → adjustment → ledger.

Each ends with a hard stop. B2 only starts after B1 lands and the user
approves.

---

# Codebase grounding

Read before designing — every claim below is grounded in existing code.

## Source tables that move coin/ounce stock

Confirmed by `grep -rn "on_hand_qty" app/` — every mutation site, no others exist:

| Direction | Source | Field | Confirmed at |
|---|---|---|---|
| **+ qty** | `SupplierPurchaseItem` | `quantity` where `item_kind = COIN`/`OUNCE` | [app/api/suppliers.py:305,323](app/api/suppliers.py#L305) |
| **+ qty** | `WalkinBuyback` | `quantity` where `kind = COIN`/`OUNCE` | [app/api/buybacks.py:287,362](app/api/buybacks.py#L287) |
| **+ qty** | `ManualAdjustment` with delta > 0 | `delta` where `target_type IN (COIN_STOCK, OUNCE_STOCK)` | [app/api/adjustments.py:243](app/api/adjustments.py#L243) |
| **+ qty** | Order void restoration | `OrderItem.quantity` re-added | [app/api/orders.py:455,473](app/api/orders.py#L455) |
| **− qty** | `OrderItem` (COMPLETED + REFUNDED) | `quantity` where `item_kind = COIN`/`OUNCE` | [app/api/orders.py:253](app/api/orders.py#L253) |
| **− qty** | `ManualAdjustment` with delta < 0 | `delta` | same row as +qty above |

### Melts are NOT a stock-mutation source (resolved before B1 ships)

Confirmed by reading [app/api/melts.py](app/api/melts.py) in full:
  - `_melt_product` takes a `Product`, not a `CoinType` / `OunceType`. Products are atomic items with no quantity concept; the transition is `Product.status = MELTED`. No coin/ounce row is touched.
  - `_melt_used_buyback` explicitly rejects `kind != USED_PRODUCT` at [melts.py:151-158](app/api/melts.py#L151-L158), so `COIN` and `OUNCE` walk-in buybacks cannot be routed through the melt endpoint.
  - `polish.py` doesn't touch coin/ounce stock either.

**Implication for reconcile math:** no melt term. If a future feature ever allows coin/ounce melting (e.g. "melt 5 sovereigns into a pure-gold lot"), it would have to either (a) decrement `on_hand_qty` directly — which would surface as drift on the next reconcile — or (b) go through `apply_manual_adjustment_core` (the same single mutation path B2's stock-take approval uses). Either way the new mutation source is observable and a one-line update to the reconcile reflects it.

### Void vs refund — only voids restore stock

Confirmed by grep + reading [orders.py:521-542](app/api/orders.py#L521): `REFUNDED` is a status-only transition that doesn't restore `on_hand_qty`. So:
  - `COMPLETED` orders subtract.
  - `REFUNDED` orders also subtract (items left the shop and didn't come back).
  - `VOIDED` orders are net-zero (sale subtracted, void added back) → **exclude entirely** from the reconcile sum rather than double-counting.

### Final reconcile arithmetic, locked in

```
expected_qty(unit_type_id) =
    Σ SupplierPurchaseItem.quantity   (item_kind matches, ref matches)
  + Σ WalkinBuyback.quantity           (kind matches, ref matches)
  + Σ ManualAdjustment.delta           (target_type matches, target_id matches)
  − Σ OrderItem.quantity               (item_kind matches, ref matches,
                                         Order.status IN (COMPLETED, REFUNDED))
```

The replay sums these and compares to `CoinType.on_hand_qty` /
`OunceType.on_hand_qty`. Anything else mutating `on_hand_qty` (e.g. a
future feature) surfaces as drift on the next reconcile — which is
the point.

## Existing supplier-balance reconcile to mirror

[app/api/inventory.py:124](app/api/inventory.py#L124) — `_compute_expected_balances()` then `select(SupplierBalance)` then diff per key. Same shape, different keys. Discord alert on drift is reused.

## ManualAdjustment is the only "this changes on_hand_qty" entry point

[app/api/adjustments.py](app/api/adjustments.py) already:

- Locks the target row with `with_for_update()`
- Mutates `on_hand_qty`
- Calls `record(event_type=EVENT_MANUAL_ADJUSTMENT, ..., payload={delta, reason, notes, ...})`
- Commits

This is what stock-take approval will use. No new path is added for inventory mutation.

---

# B1 detailed plan

## Goal

`GET /api/inventory/reconcile-units` returns drift between
ledger-derived expected qty and stored `on_hand_qty` for every active
coin type and ounce type.

## Files

- **Modify:** [app/api/inventory.py](app/api/inventory.py) — add the endpoint alongside the existing supplier reconcile.
- **Add tests:** `tests/test_inventory_reconcile_units.py`

## Implementation sketch

```python
async def _expected_unit_qty(db, *, kind: Literal["COIN", "OUNCE"], unit_type_id: str) -> int:
    """Sum every event that affected this unit's on_hand_qty, in
    isolation from `on_hand_qty` itself. Returns the qty that the
    history implies should be on hand."""
    # +supplier_purchase_items.quantity
    # +walkin_buybacks.quantity
    # +manual_adjustments.delta  (where target_type matches)
    # -order_items.quantity      (where Order.status = COMPLETED)
    # (-melts of this unit, if applicable — research during impl)

@router.get("/reconcile-units")
async def reconcile_units(
    alert: bool = False,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    drifts = []
    for ct in await select(CoinType).scalars():
        expected = await _expected_unit_qty(db, kind="COIN", unit_type_id=ct.id)
        if expected != ct.on_hand_qty:
            drifts.append({...})
    # same for ounce types
    if drifts and alert:
        await send_discord_alert(...)
    return {"unit_drifts": drifts, "drift_count": len(drifts), "alerted": alerted}
```

## Performance honesty

This is N+1: one expected-qty query per type. For the current dev DB
(handful of types, hundreds of events) it runs in well under a second.
At ten thousand types × millions of events, this would need batching
into single grouped queries. **Document the limit in the docstring**;
do not over-engineer now.

## Tests

Single integration test with the DB fixture:
- Seed 1 coin type with `on_hand_qty=10`
- Seed events: +20 from a supplier purchase, −5 from a sale, +1 from a
  buyback, −2 from a manual adjustment, +0 from a depleted-status melt
- Expected: 20 − 5 + 1 − 2 = 14
- Force the stored `on_hand_qty` to 14 → reconcile reports no drift
- Force stored to 13 → reconcile reports drift of −1
- Force stored to 100 → reconcile reports drift of +86

## Frontend

One button in [src/app/admin/inventory/page.tsx](jewelry_frontend/src/app/admin/inventory/page.tsx) or the existing inventory dashboard: "Reconcile coin/ounce stock". Calls the endpoint, renders the drift list. Reuses the existing supplier-reconcile UI shape (same admin page already has the supplier version per [/admin/inventory/alerts](jewelry_frontend/src/app/admin/inventory/alerts/page.tsx)).

## Definition of done

- Endpoint returns 200 with drift list for an admin
- 403 for cashier
- Live smoke: run against dev DB, confirm result against a known seed
- Frontend button works and renders drift table

🛑 **Hard stop. Review. Then B2.**

---

# B2 detailed plan

## Goal

An admin can record a physical count of coin/ounce stock; the system
computes the variance per line; a (later: manager, today: admin)
reviewer accepts or rejects each variance. Accepting a variance posts a
`ManualAdjustment` — which writes to the chained ledger via the existing
path. Rejecting leaves stock unchanged with a recorded reason.

## State machine

```
                ┌──────────┐
                │  DRAFT   │  admin creates; can add/edit lines
                └────┬─────┘
                     │ POST /stock-takes/{id}/submit
                     ▼
                ┌──────────┐
                │SUBMITTED │  variances computed & frozen; no more line edits
                └────┬─────┘
                     │ POST /stock-takes/{id}/lines/{line_id}/approve
                     │ POST /stock-takes/{id}/lines/{line_id}/reject
                     ▼
       per line  ┌──────────┐
       resolved  │APPROVED  │  ManualAdjustment posted; on_hand_qty updated
                 │REJECTED  │  variance recorded with reason; stock unchanged
                 │NO_VAR.   │  (auto-set when counted == expected)
                 └────┬─────┘
                      │ all lines resolved
                      ▼
                ┌──────────┐
                │  CLOSED  │  final state; immutable
                └──────────┘
```

**Critical invariants** (enforced server-side, tested):

1. Once `SUBMITTED`, a line's `expected_qty` (the snapshot of `on_hand_qty` at submit time) is frozen — even if `on_hand_qty` later changes from concurrent sales, the variance shown is the variance at submit time. Otherwise the operator can't trust what they're approving.
2. A line in `APPROVED` cannot be re-approved or rejected. Same for `REJECTED`.
3. `CLOSED` only when every line is resolved. No half-closed states.
4. Approving a line is idempotent at the API layer: if called twice, the second call returns 409 "already approved" — does NOT post a second adjustment.
5. The `ManualAdjustment` row created by an approval is FK-linked back to the `StockTakeLine`. Both the ledger event AND the stock-take row reference each other.

## Data model

### New table: `stock_takes`

```python
class StockTake(Base):
    __tablename__ = "stock_takes"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_by_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[StockTakeStatus] = mapped_column(
        Enum(StockTakeStatus, name="stocktakestatus_enum"),
        nullable=False,
        default=StockTakeStatus.DRAFT,
    )
    notes: Mapped[str | None]

    lines: Mapped[list["StockTakeLine"]] = relationship(back_populates="stock_take")
```

### New table: `stock_take_lines`

```python
class StockTakeLine(Base):
    __tablename__ = "stock_take_lines"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    stock_take_id: Mapped[str] = mapped_column(String, ForeignKey("stock_takes.id"), nullable=False)
    ref_type: Mapped[StockTakeRefType] = mapped_column(  # COIN_STOCK or OUNCE_STOCK
        Enum(StockTakeRefType, name="stocktakereftype_enum"), nullable=False,
    )
    ref_id: Mapped[str] = mapped_column(String, nullable=False)  # coin_type_id or ounce_type_id
    counted_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    # Set when stock-take is submitted; snapshot of on_hand_qty at that moment.
    expected_qty_at_submit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # variance = counted - expected (signed). Computed at submit; persisted
    # so the approval view doesn't have to re-derive.
    variance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolution: Mapped[StockTakeLineResolution] = mapped_column(
        Enum(StockTakeLineResolution, name="stocktakelineresolution_enum"),
        nullable=False, default=StockTakeLineResolution.PENDING,
    )
    rejection_reason: Mapped[str | None]
    # FK to the ManualAdjustment posted on approval. NULL if PENDING/REJECTED/NO_VARIANCE.
    adjustment_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("manual_adjustments.id"), nullable=True,
    )
    resolved_at: Mapped[datetime | None]
    resolved_by_user_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id"), nullable=True,
    )

    stock_take: Mapped["StockTake"] = relationship(back_populates="lines")
```

### Two new enums

- `StockTakeStatus`: `DRAFT, SUBMITTED, CLOSED`
- `StockTakeLineResolution`: `PENDING, APPROVED, REJECTED, NO_VARIANCE`
- `StockTakeRefType`: `COIN_STOCK, OUNCE_STOCK` (mirrors `AdjustmentTarget` subset)

### Should these tables be append-only?

**No** — these are workflow tables, not audit. They legitimately mutate:
draft lines get edited, status moves forward. The append-only invariant
is on the **ledger** (already done in A1/A2). Stock-take rows are
*about* the ledger but aren't *of* the ledger.

That said: once a line is APPROVED/REJECTED/NO_VARIANCE, it should not
become PENDING again. This is enforced at the API layer with status
checks, not DB triggers — the cost/benefit doesn't favor triggers here.

## Endpoints (all admin-only)

| Method | Path | Purpose | Body |
|---|---|---|---|
| `POST` | `/api/stock-takes` | Start a new stock-take in DRAFT | `{notes?}` |
| `POST` | `/api/stock-takes/{id}/lines` | Add a counted line | `{ref_type, ref_id, counted_qty}` |
| `PATCH` | `/api/stock-takes/{id}/lines/{line_id}` | Edit a line while DRAFT | `{counted_qty}` |
| `DELETE` | `/api/stock-takes/{id}/lines/{line_id}` | Remove a line while DRAFT | — |
| `POST` | `/api/stock-takes/{id}/submit` | Freeze: snapshot expected_qty, compute variance, set status=SUBMITTED. Auto-resolve zero-variance lines to NO_VARIANCE. | — |
| `POST` | `/api/stock-takes/{id}/lines/{line_id}/approve` | Post ManualAdjustment via existing path; set line APPROVED | — |
| `POST` | `/api/stock-takes/{id}/lines/{line_id}/reject` | Set REJECTED with reason | `{reason}` |
| `GET` | `/api/stock-takes` | Paged list | filters: status, date range |
| `GET` | `/api/stock-takes/{id}` | Detail with all lines | — |

## Ledger events

New event-type constants (added to `app/core/ledger.py`):

- `STOCK_TAKE_STARTED` — payload `{notes}`
- `STOCK_TAKE_SUBMITTED` — payload `{line_count, variance_count, total_variance_abs}`
- `STOCK_TAKE_LINE_APPROVED` — payload `{ref_type, ref_id, counted_qty, expected_qty, variance, adjustment_id}`
- `STOCK_TAKE_LINE_REJECTED` — payload `{ref_type, ref_id, counted_qty, expected_qty, variance, reason}`
- `STOCK_TAKE_CLOSED` — payload `{approved_count, rejected_count, no_variance_count}`

**Critical:** approving a line creates BOTH a `STOCK_TAKE_LINE_APPROVED` event AND triggers a `MANUAL_ADJUSTMENT` event (via the existing `adjustments` path). They share a transaction. The ledger thus shows the audit trail twice — once at the workflow level (line approved) and once at the inventory level (delta applied). The `MANUAL_ADJUSTMENT` payload includes `stock_take_line_id` so the two are cross-referenced.

## How approval mutates on_hand_qty — the careful part

```python
@router.post("/stock-takes/{stock_take_id}/lines/{line_id}/approve")
async def approve_line(stock_take_id, line_id, ..., user, db):
    # 1. Load + lock the line; verify it's in SUBMITTED state with PENDING
    #    resolution. with_for_update so a concurrent approve gets 409.
    line = await db.execute(
        select(StockTakeLine)
        .where(StockTakeLine.id == line_id, StockTakeLine.stock_take_id == stock_take_id)
        .with_for_update()
    ).scalar_one_or_none()
    if not line:
        raise HTTPException(404)
    if line.resolution != PENDING:
        raise HTTPException(409, f"line already {line.resolution.value}")

    # 2. Re-verify the stock-take is in SUBMITTED (not CLOSED).
    ...

    # 3. Apply the variance via the EXISTING adjustments path.
    #    We DO NOT call the HTTP handler — we call the underlying core
    #    function that mutates on_hand_qty and writes the ledger row.
    #    A new helper `apply_manual_adjustment_core(db, *, ...)` lives in
    #    app/api/adjustments.py (refactored out of the existing handler
    #    so both the HTTP endpoint and this approver use the same code).
    adjustment = await apply_manual_adjustment_core(
        db,
        target_type=line.ref_type,  # COIN_STOCK or OUNCE_STOCK
        target_id=line.ref_id,
        delta=line.variance,
        reason=AdjustmentReason.CORRECTION,
        notes=f"Stock-take {stock_take_id} approval (line {line_id})",
        actor_user_id=user.id,
        # extra payload that survives into the ledger event
        ledger_extra={"stock_take_line_id": line.id},
    )

    # 4. Mark the line APPROVED with FK back to the adjustment.
    line.resolution = APPROVED
    line.adjustment_id = adjustment.id
    line.resolved_at = now()
    line.resolved_by_user_id = user.id

    # 5. Write the workflow-level ledger event.
    await record(
        db,
        event_type=EVENT_STOCK_TAKE_LINE_APPROVED,
        actor_user_id=user.id,
        ref_type="stock_take_line",
        ref_id=line.id,
        payload={
            "stock_take_id": stock_take_id,
            "ref_type": line.ref_type.value,
            "ref_id": line.ref_id,
            "counted_qty": line.counted_qty,
            "expected_qty": line.expected_qty_at_submit,
            "variance": line.variance,
            "adjustment_id": adjustment.id,
        },
    )

    # 6. If all lines are now resolved, close the stock-take and emit
    #    STOCK_TAKE_CLOSED.
    ...

    await db.commit()
    return _to_out(line)
```

The discipline that prevents the backdoor: **`apply_manual_adjustment_core` is the only code that touches `on_hand_qty`**. The HTTP `POST /adjustments` handler calls it. The stock-take approver calls it. A future feature would call it. Any new mutation path is a code-review red flag.

## Three B2-specific tests added per user review

In addition to the integration tests below:

### Chain contiguity with N>1 chained writes per transaction (NEW)

Approving a line emits `MANUAL_ADJUSTMENT` + `STOCK_TAKE_LINE_APPROVED`
(and conditionally `STOCK_TAKE_CLOSED` if it's the last line). Each is a
`record()` call, each acquires the chain-head `SELECT FOR UPDATE`, all in
one transaction. This pattern hasn't been exercised before — every prior
caller has done at most one chained write per tx. **Test:** approve a
line that results in 3 chained writes in one tx, then run
`GET /api/ledger/verify`. Assert those 3 rows are contiguous in the
chain (each one's `prev_hash` = previous one's `entry_hash`, no break,
chain still intact). If `record()` has any subtle re-read bug on the
head row when called multiple times in the same session, this surfaces it.

### Rejected variances remain visible as live drift (NEW)

A rejected −2 variance means the system stays knowingly wrong by 2.
That's an acceptable audit outcome IF it stays surfaced; it's a hidden
defect if it doesn't. **Test:** seed coin type with stored
`on_hand_qty = 10`, ledger-replay expected = 12 (so drift of −2). Run
a stock-take, count = 10, submit. Reject the variance with reason
"acceptable shrinkage". Then call `GET /api/inventory/reconcile-units`
and assert the −2 drift is STILL reported. Independently call
`GET /api/stock-takes` and assert the closed stock-take has a row with
`resolution=REJECTED, variance=-2, rejection_reason="acceptable
shrinkage"` visible. The drift survives both surfaces — reconcile (live
view) and stock-take history (point-in-time decision).

### Frontend feedback loop for rejected variances

The stock-take history detail screen surfaces rejected lines
prominently — they are not hidden under a generic CLOSED badge. The
"Reconcile" admin button (B1) is presented next to "Stock-take" in
the sidebar so the workflow "see drift → take physical count → resolve
or knowingly accept" is one screen apart.

## Standard tests

Pure-function tests are limited here — most of the logic is workflow/state. Integration tests against the conftest DB fixture:

1. **Happy path:** start → add lines → submit (variances computed) → approve a variance → assert `on_hand_qty` changed by exactly the variance; assert `ManualAdjustment` row created with the correct `stock_take_line_id` in its ledger payload; assert chain still verifies.
2. **No-variance auto-resolution:** submit a stock-take where counted = expected on every line → all lines `NO_VARIANCE`, no adjustments posted, stock-take auto-`CLOSED`.
3. **Reject path:** reject a variance with a reason → line goes REJECTED, `on_hand_qty` unchanged, no adjustment posted, reason captured in ledger.
4. **Double-approve is 409:** approve a line, then try to approve again → 409; no second adjustment posted; chain still has exactly one approval event for that line.
5. **Expected-qty freeze:** stock-take submitted with `on_hand_qty=10` (so `expected_qty_at_submit=10`); then a concurrent sale drops `on_hand_qty` to 8; then approving with `counted=10, expected=10, variance=0` (would be NO_VARIANCE since count = expected). The concurrent sale did its own adjustment-via-sale path — this is *correct*. The variance the operator saw at submit time is the variance acted on; the concurrent sale is separately audited.
6. **Status guards:** cannot submit a stock-take in CLOSED; cannot add lines to a SUBMITTED stock-take; cannot approve a line on a DRAFT stock-take.

## Frontend

New admin tab `/admin/stock-take` (sidebar icon: `ClipboardCheck` from lucide-react). Two screens:

1. **Counting screen** (DRAFT mode): list all active coin/ounce types with their current `on_hand_qty` shown as a hint; admin enters `counted_qty` per row; "Submit Count" button at bottom.
2. **Review screen** (SUBMITTED mode): table of lines with expected, counted, variance columns; per-row Approve / Reject buttons; reject opens a small modal for the reason; an "All resolved — close stock-take" status indicator at top.

History list of past stock-takes shows status + summary stats.

## Definition of done

- All 9 endpoints land with the contracted shape
- All 6 integration tests pass
- Live smoke: full count → submit → approve one variance → reject another → close; chain verifies; `on_hand_qty` matches the approved adjustments
- Frontend flows end-to-end
- A2 triggers still hold (stock-take tables are NOT audit tables and are intentionally mutable — but the underlying ledger events ARE chain-protected via the existing adjustment path)

🛑 **Hard stop after B1. Another hard stop after B2 backend. Another after B2 frontend.**

---

## What this plan does NOT do (out of scope, by your earlier decisions)

- No maker-checker on stock-take approval — same admin can start, submit, and approve (per "drop the maker-checker approval flow" decision in audit-readiness). When granular roles land (Roles phase), approval can be moved to MANAGER.
- No mobile/barcode-scan counting flow — admin enters numbers on the desktop UI. Barcode-scan is a future enhancement.
- No pure-gold-lot reconciliation in B1 — coin/ounce only this phase (matches the original B1 scope; lots can follow as B1b later if you want).
- No stock-take "edit after submit" path — once SUBMITTED, you can only approve/reject. If a count was wrong, start a new stock-take. (Avoids state-machine complexity.)

## Sequencing & sizing estimate

| Sub-phase | Size | Hard stop after |
|---|---|---|
| **B1** | Small (~1 endpoint, ~1 test, ~1 frontend button) | Yes |
| **B2 backend** | Medium (~2 tables, ~1 migration, ~9 endpoints, ~6 tests, refactor of adjustments core into a reusable helper) | Yes |
| **B2 frontend** | Medium (~2 screens + history list) | Yes |

Pure-gold-lot reconciliation (B1b) and the granular-roles phase remain
deferred to their own phases per the master sequencing.

---

## Resolved decisions (user, 2026-05-25)

1. **Pure-gold lots in B1?** No — defer to B1b. Keep B1 single-purpose.
2. **Stock-take cadence/reminder UI?** Skipped.
3. **Sidebar position for `/admin/stock-take`?** Between `Inventory` and `Suppliers`. Approved.
4. **Melt path for coins/ounces?** Confirmed NOT a real concern — no code path exists; documented above. No melt term in the reconcile arithmetic.
5. **Stock-take tables append-only?** No, kept mutable as planned. Audit guarantee lives in the chained adjustment events.

Starting B1 now.
