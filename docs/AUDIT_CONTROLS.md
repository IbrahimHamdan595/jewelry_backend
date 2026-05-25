# MAISON ZAHAB — Audit Controls Reference

> **Purpose.** Single-page reference for what audit-grade controls
> currently exist in the system, the invariants they uphold, and how to
> operate them. Written so a new engineer or an auditor can pick it up
> cold and answer "what stops X from being silently changed?" without
> reading every commit message.
>
> **Scope of this doc.** What was SHIPPED across the audit-hardening
> engagement (audit phases A1 → A2 → A3a → A3b → B1 → B2). The original
> forward-looking assessment is in [`/AUDIT_READINESS.md`](../AUDIT_READINESS.md);
> the per-phase implementation plans are in
> [`docs/superpowers/plans/`](superpowers/plans/).

---

## 1. What was shipped

Every row below corresponds to one or more commits on `main`. Each landed
with smoke evidence against the live Neon database and a regression test
where applicable.

| Phase | What it did | Where the logic lives |
|---|---|---|
| **A1.1** | Hash-chained every InventoryLedger row: `entry_hash = sha256(canonical(fields) ‖ prev_hash)`. `record()` `SELECT FOR UPDATE`s a single-row `inventory_ledger_chain_head` to serialize appends; chain advances atomically with the audited state change. | [`app/core/audit_chain.py`](../app/core/audit_chain.py), [`app/core/ledger.py`](../app/core/ledger.py), models `InventoryLedger` + `InventoryLedgerChainHead` |
| **A1.2** | Backfilled chain across all pre-existing rows in `(occurred_at, id)` order; tightened `prev_hash` / `entry_hash` to `NOT NULL`; added `UNIQUE` constraint on `entry_hash`; exposed `GET /api/ledger/verify`. Datetime canonicalization normalizes to UTC so the chain is invariant to DB-layer tz handling (PG vs SQLite). | Same as A1.1 + [`app/api/ledger.py`](../app/api/ledger.py) `verify_ledger` |
| **A2** | Postgres BEFORE-triggers raise on UPDATE/DELETE against `inventory_ledger`, `zakat_snapshots`, and DELETE against `inventory_ledger_chain_head` (UPDATE on the head is the legit append path). Maintenance bypass via `SET LOCAL app.ledger_maintenance = 'on'` for legitimate future migrations. Dialect-guarded; SQLite test fixture is a no-op. | [`alembic/versions/e0a8bacf7474_audit_a2_append_only_triggers.py`](../alembic/versions/e0a8bacf7474_audit_a2_append_only_triggers.py), [`app/core/audit_maintenance.py`](../app/core/audit_maintenance.py) |
| **A3a** | Six sensitive admin actions now emit ledger rows: `PATCH /api/settings`, `POST/DELETE /api/gold-price/override`, `POST /api/gold-price/refresh`, `POST /api/staff`, `PATCH /api/staff/{id}` (and `DELETE` soft-delete). Gold-rate override now requires a `reason`. Settings/staff edits write per-field `{from, to}` diffs via `field_diff()`. Password hash changes recorded as `***`/`***` (logged that, not what). Frontend override form updated. | [`app/core/ledger.py`](../app/core/ledger.py) (`field_diff`, new event types), the four routers listed |
| **A3b** | Dedicated `auth_audit_log` table with its own independent hash chain. Captures login success/failure, logout, password change. Architecture deliberately differs from the inventory ledger: writes are best-effort (`fire_auth_event` → `asyncio.create_task`), fresh DB session per task, never blocks the auth path. Failed-login `user_id` is `NULL` (no FK; claimed_email may be garbage). Real client IP from `X-Forwarded-For` (Render-trusted). 18-month retention column populated on insert; no auto-pruner yet. | [`app/core/audit_chain.py`](../app/core/audit_chain.py) (sibling `compute_auth_entry_hash`), [`app/core/auth_audit.py`](../app/core/auth_audit.py), [`app/api/auth.py`](../app/api/auth.py), [`app/api/auth_audit.py`](../app/api/auth_audit.py), models `AuthAuditLog` + `AuthAuditChainHead` |
| **B1** | `GET /api/inventory/reconcile-units` replays every event that mutates `CoinType.on_hand_qty` / `OunceType.on_hand_qty` and reports drift per type. No melt term (proven by exhaustive grep — coins/ounces cannot be melted via current code). VOIDED orders excluded entirely (net-zero); REFUNDED subtract (didn't restore stock). Frontend tab `/admin/inventory/reconcile`. | [`app/api/inventory.py`](../app/api/inventory.py) `_expected_unit_qty` + `reconcile_units`, [frontend](../../jewelry_frontend/src/app/admin/inventory/reconcile/page.tsx) |
| **B2 backend** | Physical stock-take workflow (DRAFT → SUBMITTED → CLOSED). New mutation-discipline: `apply_unit_stock_adjustment_core` (extracted from `POST /adjustments`) is the **only** path that touches `on_hand_qty` for coins/ounces. Approve emits both the workflow event AND a chained MANUAL_ADJUSTMENT via that core, in one tx. Parent-then-line lock ordering for close-race safety. Explicit `StockTakeRefType → AdjustmentTarget` mapping with completeness test. | [`app/api/stock_takes.py`](../app/api/stock_takes.py), [`app/core/stock_take.py`](../app/core/stock_take.py), [`app/api/adjustments.py`](../app/api/adjustments.py) (refactored core), models `StockTake` + `StockTakeLine` + 3 enums |
| **B2 frontend** | `/admin/stock-take` tab. Counting screen makes Save / Submit / Approve three distinctly-labeled actions with explicit "this does NOT change inventory" copy on the first two. Variance shown in plain words ("short by 2", "over by 1") via [`src/lib/variance.ts`](../../jewelry_frontend/src/lib/variance.ts). Rejected lines in closed takes get a dedicated red callout above all other content — they do not collapse under a green CLOSED badge. | [Frontend route](../../jewelry_frontend/src/app/admin/stock-take/) |

---

## 2. Invariants the system now upholds

These are statements an auditor can verify by reading the code or calling
the verify endpoints. Each is enforced at the layer noted.

### Audit-trail integrity

1. **InventoryLedger is hash-chained.** Each row's `entry_hash` is computed over its own canonical fields plus the previous row's `entry_hash`. Editing or deleting any historical row breaks the chain at exactly that row. *(Enforced: `record()` + `verify_chain()`. Verify with `GET /api/ledger/verify`.)*

2. **AuthAuditLog is independently hash-chained.** Same construction, separate chain head, so high-volume failed-login probes don't contend with inventory writes. *(Verify with `GET /api/auth-audit/verify`.)*

3. **Database-level append-only.** Postgres BEFORE-triggers raise on UPDATE/DELETE against `inventory_ledger`, `zakat_snapshots`, and DELETE against `inventory_ledger_chain_head` and `auth_audit_log`. *(Enforced: `audit_block_mutation()` trigger function from audit A2.)*

4. **Maintenance bypass is scoped, explicit, and observable.** `SET LOCAL app.ledger_maintenance = 'on'` allows mutations only within the issuing transaction. Application code never sets this; only migrations do, via the helper `enable_audit_maintenance(connection)`. A forgotten flag cannot leak across transactions.

### Inventory mutation discipline

5. **`apply_unit_stock_adjustment_core` is the sole mutation path for `CoinType.on_hand_qty` / `OunceType.on_hand_qty`.** The `POST /adjustments` HTTP handler calls it; the stock-take approver calls it. Any new code touching `on_hand_qty` outside this helper is a code-review red flag. *(Enforced: convention + grep can verify; tests prove same-tx audit emission.)*

6. **Stock-take approval emits BOTH a workflow event AND an inventory event in one transaction.** `STOCK_TAKE_LINE_APPROVED` + `COIN_STOCK_ADJUSTED` (or `OUNCE_STOCK_ADJUSTED`) chain together, cross-referenced via `adjustment_id` ↔ `stock_take_line_id` in their payloads. If either insert fails, both roll back. The N>1 chained-writes-per-tx pattern has a dedicated pre-flight regression test ([`tests/test_audit_chain_multi_write.py`](../tests/test_audit_chain_multi_write.py)).

7. **Stock-take `expected_qty_at_submit` is frozen.** Computed at submit time; subsequent concurrent sales (which mutate `on_hand_qty` through their own audited path) do not move the variance the operator is approving.

### Auth audit honesty

8. **Auth events never block the auth path.** `record_auth_event_safe()` opens its own DB session and wraps every exception. If the recorder fails, the user still logs in or out. Verified by `test_record_auth_event_safe_never_raises_on_db_error`.

9. **Failed logins are captured as claimed-but-unverified.** `user_id = NULL`, `claimed_email` set to what was submitted. No FK to `users` — failed probes for non-existent accounts are recorded as evidence, not silently dropped.

10. **Failed-login events land even when the endpoint raises.** A historical class of FastAPI bug: `BackgroundTasks` only fire when the endpoint returns successfully; raising `HTTPException` bypasses them. We use `asyncio.create_task` via `fire_auth_event()` so the recorder runs regardless. Pinned by `test_fire_auth_event_schedules_task_and_completes_after_caller_raises`.

### Sensitive admin actions

11. **Every gold-rate manual override carries a required justification.** `reason` is a Pydantic-required field (`min_length=3`) on `OverrideRequest`; empty submissions return 422. The reason is in the `GOLD_RATE_OVERRIDE_SET` ledger payload alongside the new and prior rates.

12. **Settings changes emit per-field diffs.** `SETTINGS_CHANGED` payload contains `{diff: {field: {from, to}}}` for only the keys that actually changed. Same shape for `STAFF_UPDATED`. `password_hash` changes are recorded as `***` → `***` — the fact of change is auditable; the hash itself is never exposed.

### Reconciliation visibility

13. **Drift survives rejection.** A rejected stock-take variance leaves `on_hand_qty` deliberately wrong. The next `GET /api/inventory/reconcile-units` will continue to report the drift. The closed stock-take's history shows the rejected line + reason. This guarantee is tested end-to-end in `test_rejected_variance_stays_visible_as_live_drift`.

---

## 3. API surface added

All endpoints below require admin auth (`Depends(require_admin)`).

### Read-only verifiers

| Endpoint | What it returns |
|---|---|
| `GET /api/ledger/verify` | `{status: intact \| broken \| empty, total_rows, head_row_count, head_latest_hash, computed_latest_hash, head_matches, first_break: {...} \| null}`. Walks the entire InventoryLedger chain. |
| `GET /api/auth-audit/verify` | Same shape, against the AuthAuditLog chain. |

### Read-only listings

| Endpoint | Notes |
|---|---|
| `GET /api/ledger?event_type=&ref_type=&ref_id=&since=&until=&page=&page_size=` | Paged ledger query. |
| `GET /api/auth-audit?event_type=&user_id=&claimed_email=&client_ip=&since=&until=&page=&page_size=` | Paged auth audit query. |
| `GET /api/inventory/reconcile-units?alert=false` | Coin/ounce stock drift. `alert=true` fires a Discord webhook if drift found. |
| `GET /api/inventory/reconcile` | Pre-existing supplier-balance drift (kept). |

### Stock-take workflow

| Endpoint | Purpose |
|---|---|
| `POST /api/stock-takes` | Start a new DRAFT take. Body: `{notes?}`. |
| `GET /api/stock-takes?status=&page=&page_size=` | Paged history list with summary stats per take. |
| `GET /api/stock-takes/{id}` | Detail with all lines. |
| `POST /api/stock-takes/{id}/lines` | Add a counted line. Body: `{ref_type, ref_id, counted_qty}`. DRAFT only. |
| `PATCH /api/stock-takes/{id}/lines/{line_id}` | Edit `counted_qty`. DRAFT only. |
| `DELETE /api/stock-takes/{id}/lines/{line_id}` | Remove a line. DRAFT only. |
| `POST /api/stock-takes/{id}/submit` | Freeze `expected_qty_at_submit`, compute variances, transition to SUBMITTED. Auto-CLOSE if every line is zero-variance. |
| `POST /api/stock-takes/{id}/lines/{line_id}/approve` | Post `MANUAL_ADJUSTMENT` for the variance, mark line APPROVED, possibly auto-CLOSE. |
| `POST /api/stock-takes/{id}/lines/{line_id}/reject` | Mark line REJECTED with `{reason}`. Inventory unchanged. |

### Already existed but newly admin-gated in A3

| Endpoint | Change |
|---|---|
| `POST /api/gold-price/refresh` | Now requires admin (was open). Emits `GOLD_RATE_REFRESH_TRIGGERED`. |
| `GET /api/settings` | Now requires admin (was open). |
| `POST /api/gold-price/override` | Body now requires `reason` (`min_length=3`). |

---

## 4. How to operate

### Verifying a chain

```
# Inventory chain (every state mutation across products, coins, ounces, lots, suppliers, zakat snapshots)
curl -b cookies.txt https://<backend>/api/ledger/verify

# Auth chain (logins, logouts, password changes)
curl -b cookies.txt https://<backend>/api/auth-audit/verify
```

Healthy response:
```json
{"status": "intact", "total_rows": N, "head_matches": true, "first_break": null, ...}
```

If `status == "broken"`:
```json
{
  "status": "broken",
  "first_break": {
    "id": "...",
    "expected_prev_hash": "...",
    "actual_prev_hash": "...",
    "expected_entry_hash": "...",
    "actual_entry_hash": "..."
  }
}
```

**What "broken" means**: a row at or near `first_break.id` was either edited, deleted, or inserted out-of-order via a path that bypassed `record()`. The most likely real-world causes:

1. **Someone ran SQL directly with the maintenance bypass on** but didn't run the chain rebuild afterwards. Recovery: the migration that rebuilt the chain originally is in [`alembic/versions/ad3defce8609_audit_a1_backfill_chain_tighten_not_null.py`](../alembic/versions/ad3defce8609_audit_a1_backfill_chain_tighten_not_null.py); the same recompute-from-genesis logic can be re-run inside a new migration with `enable_audit_maintenance()` set.
2. **A row was deleted at the DB level** despite the A2 trigger — implies the trigger was DROPped (only a Postgres superuser can do that). Recovery: the deleted event is gone; document the incident, recreate the trigger, and decide whether to take a fresh snapshot of state.

A `broken` result is a security incident, not a routine cleanup. Investigate before doing anything else.

### Using the maintenance bypass

Inside an Alembic migration that legitimately needs to UPDATE/DELETE audit rows:

```python
from app.core.audit_maintenance import enable_audit_maintenance

def upgrade() -> None:
    enable_audit_maintenance(op.get_bind())
    # ... your UPDATE/DELETE/INSERT statements ...
```

The flag is `SET LOCAL` — scoped to the migration's transaction, automatically cleared on COMMIT or ROLLBACK. Application code must never set it.

**If you find yourself wanting to bypass outside a migration:** stop. You're about to invalidate the chain. Write a migration instead. The chain rebuild pattern is in `ad3defce8609`.

### Reading a stock-take with rejected lines

On `/admin/stock-take`, takes that closed with one or more rejected lines show:
- A red **"Closed with rejection"** badge instead of green "Closed" in the index list
- A red callout block at the top of the detail page enumerating every rejected line, its variance in words, and the reason
- The rejected lines also appear in `GET /api/inventory/reconcile-units` as live drift on every subsequent run, **by design** — the rejection records the decision to leave the system knowingly wrong; the reconcile keeps the drift visible until someone resolves it

### Approving / rejecting a stock-take variance — what each does to inventory

| Action | What changes |
|---|---|
| **Save count** (in DRAFT) | Records the operator's physical count for one row. **No inventory change.** |
| **Submit for review** | Freezes `expected_qty_at_submit`, computes variance, moves take to SUBMITTED. Auto-resolves zero-variance lines to NO_VARIANCE. **No inventory change.** |
| **Approve variance** | Posts a `ManualAdjustment` via `apply_unit_stock_adjustment_core`. **`on_hand_qty` changes by exactly the variance.** Emits chained `MANUAL_ADJUSTMENT` + `STOCK_TAKE_LINE_APPROVED` (+ `STOCK_TAKE_CLOSED` if last line) in one tx. |
| **Reject variance** | Marks line REJECTED with the supplied reason. **`on_hand_qty` unchanged.** Drift remains visible in `/inventory/reconcile-units` indefinitely. |

---

## 5. Deferred / out of scope (by user decision, not omission)

Documented here so a future auditor or engineer doesn't waste time
"discovering" they're missing.

| Item | Status | Reason |
|---|---|---|
| **AUDITOR read-only role** | Deferred indefinitely. | Goal is internal trustworthiness, not external audit prep — see `AUDIT_READINESS.md` "Approved Scope". |
| **Maker-checker / approval flow** (A5) | Deferred indefinitely. | Logging via A3 was judged sufficient for current scale. When granular roles land, stock-take approval can be moved to `MANAGER`. |
| **Signed export endpoints** (C2) | Deferred indefinitely. | Same reason as the AUDITOR role. |
| **Pure-gold lot reconciliation** (B1b) | Future phase. | Original B1 scope was coin/ounce only; lot replay against `GoldLotConsumption` follows the same pattern and is cheap once requested. |
| **Granular roles** (`ADMIN` / `MANAGER` / `ACCOUNTANT` / `CASHIER`) | Future phase. | Sequenced last per approved plan. |
| **Stock-take auto-pruner** | Future phase. | Deletion is a deliberate A2-bypass operation; design as its own audited migration when retention requires it. |
| **DB role split** (separate `gold_app` runtime role from owner) | Documented follow-up (see §6). | Triggers already block mutations; role split is defense-in-depth. ~5-minute Neon console operation. |
| **Stock-take cadence reminder banner** | Skipped. | Not requested. |
| **Mobile / barcode-scan counting** | Skipped. | Desktop UI is sufficient. |
| **Stock-take edit-after-submit** | Skipped. | Avoid state-machine complexity; start a new take if a count was wrong. |
| **Rate-limited auth (HTTP 429) events** | Future enhancement. | slowapi short-circuits before the handler runs; would need a custom 429 handler. |
| **Auth events for arbitrary timezones** | Out of scope. | All capture is UTC via Python `datetime.now(timezone.utc)`. |

---

## 6. Role-split follow-up — exact recipe

Triggers from audit A2 already block UPDATE/DELETE on the audit tables for everyone, including the app. The role split is defense-in-depth: separates "what runs the app at request time" from "what owns the schema", so a misconfigured cron or compromised app credential can't disarm the triggers.

**Estimated time:** 5 minutes with a password manager open.

### Step 1 — Neon SQL Editor, connected as the database owner

```sql
-- Create the runtime role.
CREATE ROLE gold_app LOGIN PASSWORD '<generate-32-char-random>';

-- Schema usage, table reads/inserts on everything.
GRANT USAGE ON SCHEMA public TO gold_app;
GRANT SELECT, INSERT, UPDATE, DELETE
  ON ALL TABLES IN SCHEMA public TO gold_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO gold_app;

-- Future tables created by migrations: same defaults.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO gold_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO gold_app;

-- THE CRITICAL PART: revoke mutation rights on the audit tables.
REVOKE UPDATE, DELETE
  ON inventory_ledger,
     auth_audit_log,
     zakat_snapshots
  FROM gold_app;
REVOKE DELETE
  ON inventory_ledger_chain_head,
     auth_audit_chain_head
  FROM gold_app;
-- (UPDATE on the chain heads stays granted — record() needs to advance them.)
```

### Step 2 — Render, update the env var

```
DATABASE_URL=postgresql+asyncpg://gold_app:<password>@<host>/<dbname>?ssl=require
```

Deploy. App reconnects as `gold_app` on next request.

### Step 3 — Keep the owner credential for migrations only

Move the current owner URL out of any deployed service. Store in password manager under `MAISON ZAHAB / Neon / migration owner`. To run a migration:

```bash
DATABASE_URL='<owner-url>' alembic upgrade head
```

### Step 4 — Verify after deploy

```bash
curl -b cookies.txt https://<backend>/api/ledger/verify
# expect status=intact, head_matches=true
```

And from a psql session connected as `gold_app`, confirm:

```sql
DROP TRIGGER trg_inventory_ledger_block_update ON inventory_ledger;
-- expect: ERROR: must be owner of table inventory_ledger
```

### What this still doesn't protect against

A Postgres superuser (Neon project owner credential) can do anything,
including dropping triggers and editing rows. That credential's custody
is a process-control item: password manager + documented access list +
rotation on personnel changes. Not a code problem.

---

## 7. Reference — files added or substantially modified

For when an auditor asks "show me where X lives":

**Backend modules (audit-relevant):**
- [`app/core/audit_chain.py`](../app/core/audit_chain.py) — hash chain primitives (inventory + auth)
- [`app/core/audit_maintenance.py`](../app/core/audit_maintenance.py) — A2 bypass helper
- [`app/core/auth_audit.py`](../app/core/auth_audit.py) — best-effort auth recorder, XFF helper
- [`app/core/ledger.py`](../app/core/ledger.py) — `record()`, `field_diff()`, event-type constants
- [`app/core/stock_take.py`](../app/core/stock_take.py) — `StockTakeRefType → AdjustmentTarget` mapping

**Backend routers:**
- [`app/api/adjustments.py`](../app/api/adjustments.py) — `apply_unit_stock_adjustment_core` (sole on_hand_qty mutation path)
- [`app/api/ledger.py`](../app/api/ledger.py) — `verify_ledger`
- [`app/api/auth.py`](../app/api/auth.py) — login/logout/change-password with auth-audit hooks
- [`app/api/auth_audit.py`](../app/api/auth_audit.py) — list + verify endpoints
- [`app/api/inventory.py`](../app/api/inventory.py) — `_expected_unit_qty` + `reconcile_units`
- [`app/api/stock_takes.py`](../app/api/stock_takes.py) — full workflow

**Migrations:**
- `28b387f25187` — A1.1: chain columns + head table
- `ad3defce8609` — A1.2: backfill + NOT NULL + UNIQUE
- `e0a8bacf7474` — A2: append-only triggers
- `b66a1a3f957f` — A3b: auth_audit_log + chain + triggers
- `be276347fff9` — B2: stock_takes + stock_take_lines

**Tests (audit-relevant):**
- `tests/test_audit_chain.py` — pure chain semantics, tz invariance, tamper detection
- `tests/test_audit_chain_db.py` — chain through real DB; raw-SQL tamper detected at exact row
- `tests/test_audit_chain_multi_write.py` — N>1 record() per tx pre-flight (B2 enabler)
- `tests/test_auth_audit.py` — auth chain semantics, XFF parsing, best-effort guarantees, raise-after-fire regression
- `tests/test_field_diff.py` — SETTINGS_CHANGED / STAFF_UPDATED diff helper
- `tests/test_inventory_reconcile_units.py` — coin/ounce drift, void-vs-refund correctness
- `tests/test_stock_take_ref_type_mapping.py` — explicit mapping + completeness
- `tests/test_stock_take_flow.py` — full state machine + chain contiguity + rejected-drift-stays-visible

**Frontend:**
- [`src/lib/variance.ts`](../../jewelry_frontend/src/lib/variance.ts) — variance-in-words single source of truth
- [`src/app/admin/stock-take/`](../../jewelry_frontend/src/app/admin/stock-take/) — full workflow UI
- [`src/app/admin/inventory/reconcile/`](../../jewelry_frontend/src/app/admin/inventory/reconcile/) — drift report
- [`src/app/admin/gold-price/page.tsx`](../../jewelry_frontend/src/app/admin/gold-price/page.tsx) — override reason field

**Total backend test count after this engagement:** 92 passing (vs ~14 at the start).

---

*Last updated: 2026-05-26. If you change a control, update this doc in the same commit.*
