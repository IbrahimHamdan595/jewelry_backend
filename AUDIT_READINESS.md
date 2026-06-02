# Fawaz El Namel — Audit Readiness Assessment

> **Phase 0 deliverable.** No code changes yet. Read this, mark up what you want
> changed, and approve before implementation starts.

**Audit standard assumed:** Big-Four review of a live financial-inventory system
holding real gold and real customer/supplier money. Findings ranked by likely
auditor impact.

**Repo state reviewed:** `main` @ commit `d97a6c7` (backend) / `b4ec564` (frontend).

---

## Executive Summary

The system already has the *skeleton* of an audit-grade design: a centralized
`InventoryLedger` is written inside the same DB transaction as every state
change, Zakat snapshots are SHA-256-hashed, supplier balances can be replayed
from purchases minus payments. Compared to most jewellery POS code I've seen,
that's far above average.

But several controls are **conventional, not enforced**:

- The ledger is append-only *by convention only* (no DB grants, no triggers).
- The Zakat hash lives in the same DB as the row it protects — a DBA can
  recompute it after editing.
- Roles are binary (`ADMIN` vs `CASHIER`); a single admin can initiate, approve,
  and reconcile the same transaction.
- Several **sensitive admin actions write no ledger event at all** — most
  significantly the gold-rate manual override and all `Settings` mutations (VAT
  %, LBP rate, karat markups, nisab, buyback margin).
- No auth/access events captured (login, password change, role change,
  staff create).

These are the kinds of findings that turn a clean audit into a qualified one.
None require infrastructure work to fix in code; some additionally need infra
hardening that I call out honestly as out-of-scope for this repo.

---

## Current Controls — What Works, What an Auditor Will Push Back On

### Access control — [app/core/permissions.py](app/core/permissions.py)

**Current state.** Single helper `require_admin(user)` checks `user.role == Role.ADMIN`. The `Role` enum ([app/models/__init__.py:21](app/models/__init__.py#L21)) has exactly two values: `ADMIN`, `CASHIER`. Every sensitive endpoint guards itself with `Depends(require_admin)` — used **94 times across 16 router files** (per grep). There is no permission abstraction, only role equality checks scattered through endpoints.

**An auditor would accept:** that admin/cashier separation exists and is enforced server-side, that JWT-based auth backs it, that we recently added cookie-based auth + middleware JWT verification (the prior `mz_role` cookie bypass is closed).

**Gap.** **No segregation of duties.** A single ADMIN today can:
- create a `SupplierPurchase` ([app/api/suppliers.py:403](app/api/suppliers.py#L403))
- record a `SupplierPayment` against it ([app/api/suppliers.py:566](app/api/suppliers.py#L566))
- run `GET /inventory/reconcile` ([app/api/inventory.py:124](app/api/inventory.py#L124)) to "verify" their own work
- void any order ([orders.py — `EVENT_ORDER_VOID` flow](app/api/orders.py))
- write a manual override on the gold rate ([app/api/gold_price.py:55](app/api/gold_price.py#L55))
- post any `ManualAdjustment` (loss / theft / gift / sample / correction) ([app/api/adjustments.py:33](app/api/adjustments.py#L33))

All in the same login session, no second pair of eyes, no read-only watcher
role to give the auditor or an external accountant. **This is the single most
common Big-Four finding on systems of this maturity.**

### Audit trail — `InventoryLedger`

**Current state.** [app/core/ledger.py](app/core/ledger.py) writes `InventoryLedger` rows via `record()`. Used for: lot creation/consumption/depletion, manual adjustments, walk-in buybacks (4 kinds), sales (product/coin/ounce), order voids, product status changes, melt, polish, supplier created/updated/purchased/paid/balance-changed, coin/ounce type CRUD + stock adjusts, zakat snapshots.

Every write is inside the same DB transaction as the state change ([app/core/ledger.py:68](app/core/ledger.py#L68)) — confirmed by reading every caller. The ledger router ([app/api/ledger.py](app/api/ledger.py)) exposes read-only filters (event_type, ref_type/id, date range). **There is no PATCH or DELETE endpoint.**

**An auditor would accept:** the *intent*, the comprehensive event-type catalogue, the transactional guarantee, and the dual-key indexing (`(ref_type, ref_id)` and `event_type`).

**Gaps.**

1. **Append-only by convention only.** Per the module docstring: *"The ledger is append-only at the API layer (no UPDATE/DELETE endpoints exist)."* This is **not enforced**. Anyone with DB credentials — a DBA, a misconfigured backup-restore script, an attacker via the app's own credentials — can `UPDATE inventory_ledger SET payload = '{}' WHERE id = '…'` and silently rewrite history. The auditor's question: *"What stops a privileged actor from editing an entry post-hoc?"* Today: nothing in the database.
2. **No hash chain.** Each row is self-contained. Even if individual rows were checksummed, deletion of an entire row would be undetectable — there's no "next row" expecting to see the previous row's hash.
3. **No verification path.** No CLI, no endpoint that walks the log and certifies "rows 1..N are intact." If an auditor asks "prove the ledger hasn't been tampered with," the only available answer today is "trust us."
4. **Several sensitive actions skip the ledger entirely** — see "Coverage Gaps" below.

### Zakat snapshot integrity — [app/core/zakat.py](app/core/zakat.py), [app/api/zakat.py](app/api/zakat.py)

**Current state.** Each snapshot stores a `sha256` integrity hash over a canonical-JSON serialization of all inputs and outputs ([compute_integrity_hash, app/core/zakat.py:108](app/core/zakat.py#L108)). Read endpoints recompute and surface `integrity_ok: bool` so tampering shows in the UI. No UPDATE/DELETE endpoints exist for snapshots; tests cover determinism, change-detection, and key-order invariance.

**An auditor would accept:** the design intent and the field-by-field hash coverage. This is genuinely better than most.

**Gap.** **The hash is stored next to the data it protects.** A DBA can:
1. Edit a snapshot row.
2. Compute the new SHA-256.
3. Update `integrity_hash` to match.
4. The application reports `integrity_ok: True`.

The hash only catches **unsophisticated** tampering. To raise the bar, the hash must chain to something the local DBA can't easily forge — either a chain back to the previous snapshot's hash (so any single edit propagates forward), or an external attestation (signed publish, append-only log service).

### Reconciliation — `GET /api/inventory/reconcile`

**Current state.** [app/api/inventory.py:124](app/api/inventory.py#L124) replays `SupplierPurchase` and `SupplierPayment` rows and diffs against the stored `SupplierBalance` table. Drift fires a Discord alert if requested. Returns `(supplier_id, unit, karat) → {stored, computed, drift}` for any mismatch.

**An auditor would accept:** that supplier debt has a mechanical replay check.

**Gap.** The function's own docstring acknowledges the limitation:

> *"Coin/ounce stock reconciliation is NOT included here — sweeping the JSONB ledger payloads is expensive and the on_hand_qty column is the source of truth (it's locked FOR UPDATE on every change). Flag if you want a deeper sweep added."*

**Auditors care most about physical existence of gold**, and physical existence maps to coin/ounce on-hand counts and pure-gold lot remaining weights. The thing the reconcile *doesn't* check is the thing they'll most want checked. Also:
- **No physical stock-take feature.** No way for an admin to record "I counted 47 sovereigns in the safe" and have the system log a variance against the digital `on_hand_qty`.
- **Pure-gold lots are never reconciled** (no replay against `GoldLotConsumption`).

### Gold-rate provenance — [app/core/gold_api.py](app/core/gold_api.py), [app/api/gold_price.py](app/api/gold_price.py)

**Current state.** Live rates fetched from GoldAPI.io, fall back to a goldprice.org public endpoint. Polled every N minutes by APScheduler, stored in `GoldRateHistory`. An admin can set a manual override via `POST /gold-price/override` ([app/api/gold_price.py:55](app/api/gold_price.py#L55)); the override row stores `set_by` and `set_at`.

**An auditor would accept:** that rates are persisted with provenance (source field on each `GoldRateHistory` row), and that the override row records who set it.

**Gaps.**

1. **No ledger event on override set or clear.** `set_override` and `clear_override` mutate `GoldRateOverride` but never call `record()`. Confirmed by reading the file end-to-end. So the only record is in the override table itself, not in the audit trail an auditor would inspect.
2. **No reason field on override.** Admin can post `{"rate_24k": 999}` with no justification stored.
3. **No old-value / new-value capture.** The override replaces the prior active row (sets it to `is_active=False`) but the diff isn't logged structured.
4. **`GoldRateOverride` rows can be edited at the DB layer** with no detection — same class of issue as the ledger.

### Coverage gaps — sensitive actions with NO ledger event

I read every router. The following high-risk endpoints write nothing to the audit trail today:

| Action | Endpoint | What's missed |
|---|---|---|
| Settings change | `PATCH /api/settings` ([app/api/settings.py:21](app/api/settings.py#L21)) | VAT %, LBP exchange rate, karat markups, nisab, buyback margin defaults, gold-refresh interval — all change with no ledger trail |
| Gold-rate manual override (set) | `POST /api/gold-price/override` | See above |
| Gold-rate manual override (clear) | `DELETE /api/gold-price/override` | No ledger entry; admin can silently revert |
| Gold-rate force-refresh | `POST /api/gold-price/refresh` | Now admin-only, but no record of who triggered it |
| Staff create | `POST /api/staff` | New cashier user appears with no ledger row |
| Staff update / disable | `PATCH /api/staff/{id}` | Role/active-state change unaudited |
| Login (success or failure) | `POST /api/auth/login` | No audit at all (rate-limit is in-memory only) |
| Password change | `POST /api/auth/change-password` | Not audited |
| Logout | `POST /api/auth/logout` | Not audited |

The `voided_at` / `voided_by` columns on `Order` plus `EVENT_ORDER_VOID` give us proper coverage for voids. Sales, buybacks, supplier purchases, supplier payments, manual adjustments, melts, polishes, lot ops, coin/ounce type CRUD — all properly recorded.

### Sequence integrity — order numbers

**Current state.** `generate_order_number(db, when)` ([app/core/pricing.py:133](app/core/pricing.py#L133)) returns `ORD-YYYYMMDD-NNN` where NNN is `count(orders on that day) + 1`. `Order.order_number` has `unique=True` ([app/models/__init__.py:193](app/models/__init__.py#L193)).

**An auditor would accept:** that duplicates are blocked by the DB unique constraint.

**Gap.** Two issues:
1. **Race condition.** Two simultaneous checkouts on the same day will both compute the same `count + 1`, both attempt the same `order_number`, and the second will get a DB `IntegrityError`. The user sees a confusing error and may retry, double-creating. Not malicious, but messy. A proper solution is a Postgres `SEQUENCE` per day or a transactional counter table.
2. **Gap detection.** If an order is ever deleted (it isn't supposed to be — voids preserve the row — but the system doesn't enforce no-delete at the DB layer), the sequence will silently skip a number. There's no nightly job that says "orders 042 and 044 exist, where is 043?"

---

## Out-of-Scope (Honest Disclosure) — Process & Infra Items

These belong in the auditor's process review, not in this repo. List them in
your audit-readiness package as **business controls in progress**:

1. **Periodic user-access review.** Who reviews the list of active admins quarterly? Is there a documented offboarding checklist?
2. **Change management.** Who can deploy to prod? Is `main` protected? Is there a test environment separate from prod? Today the Render setup is a single environment per service.
3. **Backup & retention.** Where are Neon backups stored? Are they immutable? What's the retention window? Has a restore been tested in the last 12 months?
4. **Database credentials.** Who holds the production DB password directly (i.e. bypassing the application)? An auditor will assume "fewer is better."
5. **Physical stock-take procedure.** What's the cadence (annual? quarterly?), who does the count, who signs off on variance resolution, where's the paper trail?
6. **External time source for audit.** Application uses `datetime.now(timezone.utc)` — fine, but high-stakes audits sometimes want server clock attestation or NTP source documentation.
7. **Penetration testing & SOC 2 / ISO 27001 scope** — separate engagement.

I will reference these in the production code only as documentation comments where relevant; I won't pretend to "fix" them.

---

## Proposed Changes — Ranked by Audit Risk

> Each item lists the AUDIT RATIONALE (what auditor concern it answers), the
> implementation scope, and a rough impact estimate.

### A. CRITICAL — Close before audit begins

**A1. Hash-chained `InventoryLedger` + verification endpoint.**
- **Why:** answers the question *"How do you prove the ledger hasn't been altered?"* Without it, the ledger is just a log, not evidence.
- **What:** add `prev_hash: str | None` and `entry_hash: str` columns to `inventory_ledger`. On insert: compute `entry_hash = sha256(canonical_json(payload+meta) || prev_hash_of_latest_row)`. Provide `GET /api/ledger/verify` that walks the chain from genesis and reports the first break (id + expected vs got). Backfill existing rows under a genesis hash so the chain is contiguous from day one.
- **Tests:** pure-function chain construction, tamper-detection (edit one row → break detected at exactly that row), key-order invariance, chain-build determinism. Mirror the zakat-hash test style.
- **Compat:** new columns, NULL-able initially → backfill migration → set NOT NULL. Existing read endpoints unaffected.
- **Out-of-scope honesty:** an attacker with full DB access can still rewrite the chain end-to-end — but they'd have to rewrite *every* subsequent row, which is detectable by anyone who held a copy of any later hash (e.g. an emailed daily summary).

**A2. DB-level append-only enforcement on ledger + snapshots.**
- **Why:** raises the bar from "no API to mutate" to "DB itself refuses." Auditor sees it in the schema, not just the code.
- **What:** Alembic migration that:
  - `REVOKE UPDATE, DELETE ON inventory_ledger, zakat_snapshots FROM <app_role>;`
  - Adds a Postgres rule or trigger that raises on UPDATE/DELETE against these tables (defense-in-depth — even a misconfigured grant won't slip through).
- **Out-of-scope honesty:** a Postgres superuser (i.e. whoever holds the Neon admin credential) can still drop the trigger and edit. Document this and confirm who that person is — that's process-control territory.

**A3. Ledger events for the missing sensitive actions.**
- **Why:** the gaps table above. Auditor will sample sensitive endpoints and ask for the ledger row.
- **What:** add `record()` calls + new event types:
  - `SETTINGS_CHANGED` — payload includes a diff `{field: {from, to}}` for every changed field. `PATCH /api/settings`.
  - `GOLD_RATE_OVERRIDE_SET` — payload `{rate_24k, prior_rate_24k_or_null, reason}`. Add `reason: str` to the `OverrideRequest` schema, require non-empty.
  - `GOLD_RATE_OVERRIDE_CLEARED` — payload `{prior_rate_24k}`.
  - `GOLD_RATE_REFRESH_TRIGGERED` — manual force-refresh actor.
  - `STAFF_CREATED`, `STAFF_UPDATED`, `STAFF_DISABLED` — payload includes role + active state diff.
- **Compat:** purely additive. No existing behavior changes.

**A4. Granular roles + permission system + read-only AUDITOR.**
- **Why:** segregation of duties.
- **What:**
  - Extend `Role` enum: `ADMIN`, `MANAGER`, `ACCOUNTANT`, `CASHIER`, `AUDITOR`. Migrate all existing `ADMIN` users → `ADMIN` (no-op). Document each role's scope.
  - Introduce a `Permission` enum and a `role_permissions` map (single source of truth, declarative).
  - Replace `require_admin` callers with `require_permission(Permission.X)` over the course of a couple of phases — keep `require_admin` as a compatibility shim that maps to a permission set so the rollout is safe.
  - AUDITOR has read access to everything, write access to nothing — enforced at the dependency layer, not by hoping endpoints check.
- **Compat:** the shim approach means no router rewrite required up front; we migrate endpoint-by-endpoint.

**A5. Maker-checker / approval workflow on sensitive actions.**
- **Why:** even with granular roles, ensure no single user can both initiate and approve a high-risk action.
- **What:** a generic `PendingAction` table — `(id, action_type, payload, initiator_id, initiator_at, approver_id?, approver_at?, status: PENDING/APPROVED/REJECTED, rejection_reason?)`. New endpoints: `POST /api/pending-actions/{type}` to propose, `POST /api/pending-actions/{id}/approve` (must be a different user with the matching permission), `POST /api/pending-actions/{id}/reject`. Actions wired through this initially:
  - `ManualAdjustment` (theft / loss / gift adjustments)
  - `Order` void
  - `GoldRateOverride` set
  - `SupplierPayment` above a configurable threshold
  - `Settings` change to financial knobs (VAT, LBP, markups, margins)
- **Edge case to spec:** what happens for low-staffed shops where there's only one ADMIN? Either (a) maker-checker is configurable per action type, (b) a single-admin emergency-mode flag is itself audited. Recommend (b) — auditor prefers "logged and approved by exception" over "silently disabled".
- **Compat:** opt-in flag per action type, default off, turn on per-action after manual testing.

### B. HIGH — Close before audit, lower risk than A

**B1. Coin/ounce stock reconciliation (replay job).**
- **Why:** auditor wants existence proof for the physical inventory they can touch.
- **What:** offline-ish endpoint `GET /api/inventory/reconcile-units` that replays every coin/ounce ledger event (`COIN_TYPE_CREATED`, `COIN_STOCK_ADJUSTED`, supplier purchases that add coins, walk-in buybacks that add coins, sales that subtract, manual adjustments, melts/polishes) and compares the replayed total to `on_hand_qty`. Slow query, paginated by type, cacheable, fire-and-forget alert if drift.
- **Honest caveat:** the replay is only as good as the events recorded. Confirms ledger completeness, doesn't prove physical reality.

**B2. Physical stock-take feature.**
- **Why:** **only physical existence proves physical existence.** A reconcile that says "system says 47, replay says 47" is *not* the same as "we counted 47 in the safe".
- **What:** `POST /api/stock-take` with a list of `{ref_type, ref_id, counted_qty}`. Engine computes `(counted_qty - on_hand_qty)` per row, persists a `StockTake` row + line items, writes one `STOCK_TAKE_RECORDED` ledger event per line, and *requires* a manager-level approver to either (a) accept the variance and adjust on_hand_qty (writes a `STOCK_TAKE_ADJUSTMENT` event) or (b) reject with reason. Stock-takes are immutable, chain-protected (via A1).
- **Frontend:** new admin screen, ideally with a barcode-scan flow.

**B3. Auth/access audit log.**
- **Why:** standard control. Auditor will ask "show me a log of all logins for user X in the last 90 days."
- **What:** new table `auth_audit_log` (separate from the inventory ledger because the volume/retention rules differ). Rows: `LOGIN_SUCCESS`, `LOGIN_FAILED`, `LOGOUT`, `PASSWORD_CHANGED`, `STAFF_CREATED`, `STAFF_DISABLED`, `ROLE_CHANGED`. Capture: user id (or email if user not found), client IP, user-agent, timestamp, optional reason. Also include in A1's hash chain (or its own chain — TBD; I'd recommend its own chain because volume is higher).
- **Why a separate table:** load characteristics differ — auth events fire on every login including bot scans; mixing them with the inventory ledger noises up reconciliation queries.

**B4. Gold-rate override reason + diff capture.**
- Already covered as part of A3, listed here for completeness because it's also a HIGH item.

### C. MEDIUM — Strengthening, but defensible without

**C1. Order-number gap detection + transactional sequence.**
- **Why:** prevents the race condition and surfaces accidental gaps.
- **What:** swap `count() + 1` for a Postgres-side `SELECT setval(...)` or a `daily_sequences` table with row-level lock. Add a nightly job that scans for gaps within each day's sequence and writes a `SEQUENCE_GAP_DETECTED` ledger event listing the missing number. (A gap is not necessarily a problem — it can be a legitimate void that preserved the row, in which case the job won't flag — but unexplained gaps surface fast.)

**C2. Auditor-friendly signed export endpoints.**
- **Why:** auditor will want to take a copy home and verify it offline.
- **What:** `GET /api/exports/ledger?from=…&to=…&format=csv|json` returning the date-bounded ledger plus the genesis-to-end chain summary (first hash, last hash, row count). Optionally sign the export with a static RSA key whose public half ships in the auditor packet — they can verify the file hasn't been edited after delivery.

**C3. Deterministic point-in-time financial replay.**
- **Why:** "what did the books look like on March 31?"
- **What:** every snapshot/report endpoint that takes a date today should be able to take an `as_of` timestamp. The compute layer must read only ledger events with `occurred_at <= as_of` and never read mutable state. The Zakat snapshot does this implicitly because it persists everything; expand the pattern: an `as_of` parameter on supplier-balance views, AP aging, and a new "trial balance" endpoint.
- **Scope:** larger than it sounds. Defer to a later phase, but design the ledger so it's *possible*.

### D. LOWER — Document and address opportunistically

**D1. Pure-gold lot replay reconciliation.** Same approach as B1, applied to `GoldLotConsumption` → `GoldLot.weight_remaining_grams`.

**D2. Code-side docstrings citing the AUDIT rationale.** Every new control gets a docstring header explaining what auditor concern it addresses. Existing modules — especially `ledger.py`, `zakat.py`, `permissions.py` — get the same treatment so an auditor reading the code understands intent without spelunking through git history.

**D3. NOT NULL drift cleanup migration** (carried over from the zakat phase — pre-existing latent issue on legacy timestamp columns, see prior plan).

---

## Recommended Sequencing

| Sprint | Items | Why this order |
|---|---|---|
| 1 (week 1) | **A3** (ledger events for gaps) + **A1** (hash chain core) | Highest auditor visibility, both small surface, both pure-add. A3 has no dependencies; A1 changes a schema. |
| 2 (week 2) | **A2** (DB-level append-only) + **A4** (granular roles + AUDITOR) | A2 piggybacks on the same Alembic momentum. A4 unblocks B-tier work. |
| 3 (week 3) | **A5** (maker-checker) + **B3** (auth audit log) | A5 needs A4 in place. B3 is independent but small. |
| 4 (week 4) | **B1** + **B2** (reconcile and stock-take) | Bigger features, depend on A4 (role-gated approval). |
| 5+ | C-tier, D-tier | After A and B are deployed and reviewed. |

Each sprint ends with a regression run, a manual smoke against staging, and a
short audit-impact summary appended to this document so the auditor can read
top-to-bottom and see what landed when.

---

## Approved Scope (2026-05-25)

**Goal:** internal trustworthiness, not external audit prep.

**In scope, in this order:**
1. **A1** — Hash-chained `InventoryLedger` + verify endpoint
2. **A2** — DB-level append-only enforcement (revoke + triggers)
3. **A3** — Ledger events for the 9 sensitive actions that write nothing today (including auth events)
4. **B1 + B2** — Coin/ounce stock reconcile + physical stock-take with variance workflow
5. **Roles last** — Replace binary `ADMIN`/`CASHIER` with `ADMIN` / `MANAGER` / `ACCOUNTANT` / `CASHIER`. Cashier cannot touch financials; Accountant can read/post supplier purchases and payments but not change settings or gold-rate overrides; Manager can void/refund but not change settings; Admin retains everything.

**Out of scope:**
- AUDITOR read-only role
- Maker-checker / approval workflow (A5) — logging via A3 is the chosen control
- Signed export endpoints (C2)
- Process / infra disclosures section (this is internal, not for external auditors)
- C/D-tier polish items (will reconsider after the above land)

**Cadence:** same as the zakat engagement — phased, TDD where the logic warrants tests, hard stop with smoke summary at the end of each phase, no Phase N+1 until Phase N is approved.

## Follow-up: DB role split (defer of audit A2)

Status: NOT YET APPLIED. The A2 migration installed Postgres triggers that
already block UPDATE/DELETE on the audit tables for everyone (including the
app), so this is now a defense-in-depth hardening, not a gap that blocks
the audit. Schedule when you next have ~10 minutes in the Neon console.

### Why this is still worth doing

The triggers can be dropped by any Postgres role that owns the tables —
today, the app's own role does. A misconfigured cron, a stray
`alembic downgrade`, or a compromised app credential could still call
`DROP TRIGGER trg_inventory_ledger_block_update ON inventory_ledger;`
followed by `UPDATE`. Splitting roles separates "what runs the app at
request time" from "what owns the schema", so the only credential that
can disarm the triggers is one a human pulls out of a password manager
to run migrations — not one sitting in `DATABASE_URL` on a web service.

### Concrete steps (5 minutes once the password manager is open)

**1. Neon — create a low-privilege app role** (Neon console → SQL Editor,
connected as the database owner):

```sql
-- Create the runtime role
CREATE ROLE gold_app LOGIN PASSWORD '<generate-32-char-random>';

-- Schema usage, table reads/inserts on everything
GRANT USAGE ON SCHEMA public TO gold_app;
GRANT SELECT, INSERT, UPDATE, DELETE
  ON ALL TABLES IN SCHEMA public TO gold_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO gold_app;

-- Future tables created by migrations: same defaults
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO gold_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO gold_app;

-- THE CRITICAL PART: revoke mutation rights on the audit tables.
-- The triggers already block these too, but defense-in-depth means even
-- if a future migration accidentally drops the triggers, gold_app still
-- can't touch the rows.
REVOKE UPDATE, DELETE
  ON inventory_ledger,
     zakat_snapshots
  FROM gold_app;
REVOKE DELETE
  ON inventory_ledger_chain_head
  FROM gold_app;
-- (UPDATE on chain_head stays granted — record() needs to advance the head.)
```

**2. Render — point the app at the new role.** In the Render dashboard
for the backend service, update the env var:

```
DATABASE_URL=postgresql+asyncpg://gold_app:<password>@<host>/<dbname>?ssl=require
```

(Same host/db, only the user + password change.) Deploy. The app will
reconnect as `gold_app` on next request.

**3. Keep the existing owner credential for migrations only.** The
current `DATABASE_URL` value (the owner role) stops living in any
deployed service. Move it to your password manager under
`Fawaz El Namel / Neon / migration owner`. To run migrations:

```bash
DATABASE_URL='<owner-url>' alembic upgrade head
```

(Or set a separate `DATABASE_URL_ADMIN` in your local `.env` so you don't
have to paste the password each time.)

**4. Verify after deploy.** Once Render redeploys with the new
`DATABASE_URL`, sanity-check from the app:

```
curl -b cookies.txt https://<backend>.onrender.com/api/ledger/verify
# expect status=intact, head_matches=true
```

And confirm the privilege downgrade actually took by trying a write the
app SHOULDN'T be able to do, e.g. attempting `DROP TRIGGER` from a psql
session connected as `gold_app`:

```
ERROR:  must be owner of table inventory_ledger
```

### What this still doesn't protect against

A Postgres superuser — whoever holds the Neon project owner credentials —
can do anything, including dropping triggers, granting themselves
privileges, and editing rows. That credential's custody is a
process-control item: keep it in a password manager, document who has
access, rotate on personnel changes.

---

## What I Need From You Before Phase 1

1. **Sign off (or amend) this ranked list.** If you'd rather re-order, do so — but please don't drop anything in tier A.
2. **Confirm the role taxonomy.** ADMIN / MANAGER / ACCOUNTANT / CASHIER / AUDITOR — or do you want different names / a different split?
3. **Confirm maker-checker scope.** Are the five action types in A5 the right starting set? Want any added or removed?
4. **Single-admin emergency mode** — recommend a "logged-bypass" toggle for shops with only one admin user. Confirm acceptable, or you'd rather require >=2 admins always.
5. **DB role for app vs DBA.** Today the app likely connects as a Neon database owner. A2 implies splitting: an `app` role with no UPDATE/DELETE on audit tables, and a separate higher-privilege role for migrations. Confirm you want the split now (and we add it to the deploy playbook) or later.
6. **Auth log retention** — propose 18 months. Confirm or change.
7. **Cadence of stock-takes** (B2) — propose: monthly for coins/ounces, quarterly for products, annual full audit. Confirm or amend.

Once you sign off, I open Phase 1 (sprint 1: A3 + A1) following the same TDD +
phased + hard-stop pattern as the zakat work, and I do not start Phase 2 until
you've reviewed Phase 1.
