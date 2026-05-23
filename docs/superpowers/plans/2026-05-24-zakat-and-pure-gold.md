# Zakat & Pure Gold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new admin-only "Zakat & Pure Gold" sidebar tab that computes total pure Au across all four inventory types (products, coins, ounces, lots), shows the 2.5% zakat due in grams + cash, compares to a configurable nisab, and supports immutable dated snapshots.

**Architecture:** A single `compute_zakatable_holdings()` service is the *only* place that decides what gold counts toward the total. The API, snapshot model, and screen consume its output. Switching from gross (this pass) to net-of-supplier-debt (future) is a localized edit to that one function — no schema, API, or UI change required.

**Tech Stack:** FastAPI + SQLAlchemy 2.x async + Alembic (backend). Next.js App Router + SWR + Tailwind + lucide-react (frontend). Reuses existing `KARAT_PURITY` map, `get_current_gold_rate()`, `require_admin`, `Settings` singleton, and the sidebar `NAV` array pattern.

---

## Codebase Findings & Assumptions

### Backend
- **ORM:** SQLAlchemy 2.x async (Mapped/mapped_column) — [pyproject.toml](../../pyproject.toml). All models in [app/models/__init__.py](../../app/models/__init__.py).
- **Migrations:** Alembic, versions in [alembic/versions/](../../alembic/versions/) — 9 existing files following pattern `<hash>_<phase_description>.py`. New migrations append.
- **Audit ledger:** `InventoryLedger` ([models:354](../../app/models/__init__.py#L354)) — keyed by `event_type`, `ref_type/ref_id`, JSON `payload`. **Zakat snapshots will NOT use this** — they need structured per-karat breakdown columns, so a dedicated `zakat_snapshots` table is correct.

### The Four Inventory Types — How to Sum Au

| Type | Model | Weight Field | Karat Field | "Still on hand" filter | Au calculation |
|---|---|---|---|---|---|
| Products | `Product` [models:152](../../app/models/__init__.py#L152) | `weight_grams Numeric(10,3)` | `karat Karat` | `status IN (AVAILABLE, RESERVED)` — see TODO note in Phase 2.4 | `weight_grams * purity` |
| Coins | `CoinType` [models:393](../../app/models/__init__.py#L393) | `weight_grams Numeric(10,3)` (per coin) | `karat Karat` | `on_hand_qty > 0` | `weight_grams * on_hand_qty * purity` |
| Ounces | `OunceType` [models:418](../../app/models/__init__.py#L418) | `weight_grams Numeric(10,3)` (per bar) | `karat Karat` | `on_hand_qty > 0` | `weight_grams * on_hand_qty * purity` |
| Pure gold lots | `GoldLot` [models:309](../../app/models/__init__.py#L309) | `weight_remaining_grams Numeric(10,3)` | `karat Karat` | `is_depleted.is_(False)` | `weight_remaining_grams * purity` |

**Critical:** for products, `is_active` is *not* the right filter — use `status == AVAILABLE`. `SOLD`/`MELTED` products no longer exist physically. For coins/ounces, include all rows with `on_hand_qty > 0` regardless of `is_active` (Au sitting in a deactivated type is still Au on hand).

### Karat & Purity
- **`Karat` enum** ([models:26](../../app/models/__init__.py#L26)): K18, K21, K22, K24. ✅ K22 included.
- **`KARAT_PURITY` map** exists in [app/core/pricing.py:9](../../app/core/pricing.py#L9):
  - K18 = 0.750, K21 = 0.875, K22 = 0.917, K24 = **0.999** ⚠️
- **⚠️ Spec says K24 = 0.9999.** Existing map uses 0.999. **Decision point for the user:** keep existing 0.999 (consistent with current pricing) or update to 0.9999 (matches spec)? See "Open Questions" #1.
- **Scattered purity factors (flag only — out of scope this pass):**
  - [app/api/gold_price.py:22-23](../../app/api/gold_price.py#L22) hardcodes `r * 0.875` and `r * 0.750`.
  - Frontend [src/lib/utils.ts](../../../jewelry_frontend/src/lib/utils.ts) has its own `KARAT_PURITY` (K24 = 0.999, **missing K22**).
  - The zakat code will **reuse `KARAT_PURITY` from `pricing.py`** exclusively. Scattered constants stay as-is; cleanup is a future hygiene task.

### Live Gold Rate
- `get_current_gold_rate(db) -> {rate, source, fetched_at, is_stale}` in [app/core/gold_api.py:68](../../app/core/gold_api.py#L68). Prefers manual override, else latest poller history; sets `is_stale=True` if data is > 15 min old; raises `RuntimeError` if no data at all. **Zakat code MUST call this** — never refetch.

### Settings Pattern
- Singleton row `id="singleton"` in `Settings` model [models:273](../../app/models/__init__.py#L273). Exposed via `SettingsOut` / `SettingsUpdate` in [app/schemas/settings.py](../../app/schemas/settings.py); router in [app/api/settings.py](../../app/api/settings.py) handles GET/PATCH. New `nisab_grams Numeric(10,3)` column follows this same shape.

### Auth
- `Depends(require_admin)` from [app/core/permissions.py:7](../../app/core/permissions.py#L7). Used on every zakat endpoint.

### Supplier Gold Debt (for future net-of-debt switch)
- `SupplierBalance` ([models:572](../../app/models/__init__.py#L572)) — composite PK `(supplier_id, unit, karat)`. Rows with `unit == DebtUnit.GOLD` and `karat ∈ {K18,K21,K22,K24}` give grams of gold owed per karat per supplier. The future "net-of-debt" version of `compute_zakatable_holdings()` will sum across all suppliers per karat and subtract from the gross karat totals. **Schema is already sufficient.**

### Frontend
- **Sidebar:** [src/app/admin/layout.tsx:24-35](../../../jewelry_frontend/src/app/admin/layout.tsx#L24) — `NAV` array of `{href, icon, label: t.nav.X}`. New entry slots in here, after "Gold Price" and before "Settings".
- **Icon library:** lucide-react. Will use `Scale` (or `Coins`) icon for zakat.
- **Auth on admin shell:** middleware-based (JWT verify) — already in place, no change needed.
- **Data fetching:** SWR with `apiFetcher` from [src/lib/api-client.ts](../../../jewelry_frontend/src/lib/api-client.ts). Existing admin pages call `useSWR<T>("/path", apiFetcher)`.
- **i18n:** Strictly typed in [src/i18n/en.ts](../../../jewelry_frontend/src/i18n/en.ts). Adding `nav.zakat` requires editing the `nav` interface + `en.ts` + `ar.ts`.
- **Table/charts:** recharts and shadcn primitives are present per `package.json` audit; the per-karat breakdown can be a plain styled table — no chart needed for v1.
- **Formatting helpers:** [src/lib/utils.ts](../../../jewelry_frontend/src/lib/utils.ts) has `cn()` and `calculatePrice()` but no dedicated currency/weight formatter — Intl.NumberFormat inline is fine, matching what other pages already do.

### Resolved Decisions (from user, 2026-05-24)
1. **K24 = 0.999** — single source of truth, keep `KARAT_PURITY` as-is in `pricing.py`. No change to existing pricing engine.
2. **Include `is_used == True`** products — they're still gold on hand.
3. **`RESERVED` products: include.** Confirmed: `ProductStatus.RESERVED` is defined in the enum but **never written anywhere in the codebase today** — no flow currently sets it. Including it in zakatable holdings is the safe default (Au still physically on hand). A `# TODO` comment in the query will flag this so whoever implements a reserve flow later (e.g. paid-awaiting-pickup) revisits the inclusion rule.
4. **Default nisab = 85.000 g.**
5. **Allow duplicate-date snapshots**; the snapshots list UI shows latest-per-date by default with an "all" toggle.
6. **Arabic keys filled in Phase 6** (user will supply zakat terms); flag for self-review.

→ Inventory filters in the per-type table above update accordingly: products use `status IN (AVAILABLE, RESERVED)` (NOT `is_active` and NOT `status == AVAILABLE` alone).

---

## File Structure

### Backend — files to create
- `app/core/zakat.py` — the **single isolation point**. Holds `compute_zakatable_holdings()` and `compute_zakat_summary()`. Net-of-debt switch lands here.
- `app/api/zakat.py` — FastAPI router. `GET /api/zakat`, `POST /api/zakat/snapshots`, `GET /api/zakat/snapshots`, `GET /api/zakat/snapshots/{id}`.
- `app/schemas/zakat.py` — Pydantic response/request models.
- `alembic/versions/<hash>_zakat_nisab_and_snapshots.py` — schema migration.
- `tests/test_zakat.py` — unit + endpoint tests.

### Backend — files to modify
- `app/models/__init__.py` — add `nisab_grams` column on `Settings`; add new `ZakatSnapshot` model.
- `app/schemas/settings.py` — add `nisab_grams` to `SettingsOut` + `SettingsUpdate`.
- `app/main.py` — register `zakat.router`.

### Frontend — files to create
- `src/app/admin/zakat/page.tsx` — the screen.
- `src/types/zakat.ts` (or extend `src/types/api.ts`) — TS types matching backend schemas.

### Frontend — files to modify
- `src/app/admin/layout.tsx` — add nav entry.
- `src/i18n/en.ts` — add `nav.zakat` to interface + value; add `zakat: {...}` section for page copy.
- `src/i18n/ar.ts` — Arabic translations.
- `src/types/api.ts` — extend `Settings` interface with `nisab_grams`.
- `src/app/admin/settings/page.tsx` — add nisab input under Default Pricing tab.

---

## The Single Isolation Point

```python
# app/core/zakat.py

async def compute_zakatable_holdings(db: AsyncSession) -> ZakatHoldings:
    """
    THE SWITCHPOINT.

    GROSS (this pass): sum Au across all four inventory types, no deductions.

    FUTURE NET-OF-DEBT: after summing, subtract grams owed to suppliers per
    karat (query SupplierBalance where unit=GOLD), clamping each karat to >= 0.
    Touch ONLY this function. The summary, snapshot, API, and screen do not
    change.
    """
    ...
```

`compute_zakat_summary()` and every consumer downstream takes a `ZakatHoldings` and is agnostic to gross vs net.

---

## Edge Cases & Rounding Strategy

| Edge case | Handling |
|---|---|
| Item with `weight_grams = 0` or `on_hand_qty = 0` | Contributes 0 Au. No special case. |
| Lot with `weight_remaining_grams = 0` but `is_depleted = False` | The filter `is_depleted == False` accepts it; sum naturally yields 0. Don't add a redundant `weight_remaining_grams > 0` clause — the existing depletion bookkeeping is authoritative. |
| `karat` value not in `KARAT_PURITY` | Cannot happen — column is non-null `Enum(Karat)`. Defensively, `compute_zakatable_holdings()` raises if it ever sees one (will surface as 500; preferable to silent miscalculation). |
| `get_current_gold_rate` raises (no rate at all) | `GET /api/zakat` returns 503 with `{"detail": "Gold rate unavailable — run the poller or set a manual override"}`. Snapshot endpoint refuses (cannot persist a snapshot without a valid rate). |
| `is_stale == True` (rate > 15 min old) | Surface the flag in the response (`gold_rate_is_stale: true`) and show a warning banner in the UI. Do NOT block computation. |
| Snapshot taken mid-transaction (e.g. simultaneous sale) | Wrap the snapshot creation in a transaction that re-reads all four tables inside the same `BEGIN`. Postgres default isolation (`READ COMMITTED`) is acceptable — we accept eventual-consistency where a sale that commits during our read may or may not be reflected. Document this in the snapshot's `notes` field if needed. **Not** worth `SERIALIZABLE` — the snapshot is a point-in-time best-effort for zakat assessment, not a regulatory ledger. |
| Nisab unset / 0 | Validation: `nisab_grams > 0` required at the schema layer. The summary still computes; `meets_nisab` is just `total_au_grams >= nisab_grams`. |
| Float drift across many rows | Use `Decimal` throughout backend. **Weight rounding:** 3 decimal places (matches existing `Numeric(10,3)` schema). **Cash rounding:** 2 decimal places, `ROUND_HALF_UP` (matches `app/core/pricing.py:_round`). Round only at the *response boundary*, never during intermediate sums. |
| Currency: stored cash values in snapshot | Numeric(14, 2) — gives headroom past $999B for inflation paranoia. |

---

## Data Model Changes

### Settings — add column
```python
# app/models/__init__.py (additive)
nisab_grams: Mapped[Decimal] = mapped_column(
    Numeric(10, 3), nullable=False, default=Decimal("85.000")
)
```

### New table: `zakat_snapshots`
```python
# app/models/__init__.py (new class)
class ZakatSnapshot(Base):
    __tablename__ = "zakat_snapshots"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    assessment_date: Mapped[date] = mapped_column(Date, nullable=False)  # admin-specified
    taken_by_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)

    # Snapshotted inputs
    gold_rate_24k_usd_per_gram: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    gold_rate_source: Mapped[str] = mapped_column(String, nullable=False)  # 'goldapi'|'lbma'|'override'
    nisab_grams_used: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)

    # Computed outputs
    total_au_grams: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    total_au_value_usd: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    zakat_au_grams: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    zakat_value_usd: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    meets_nisab: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # Full breakdown for audit — structured JSON
    # Shape: {"K18": {"products": "...", "coins": "...", "ounces": "...", "lots": "...", "total_grams": "...", "au_grams": "..."}, ...}
    breakdown_by_karat: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # Provenance hash — sha256 of breakdown_by_karat + inputs, for tamper detection.
    integrity_hash: Mapped[str] = mapped_column(String, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_zakat_snapshots_assessment_date", "assessment_date"),
        Index("ix_zakat_snapshots_taken_at", "taken_at"),
    )
```

**Immutability enforcement:**
- No UPDATE or DELETE endpoints exposed.
- `integrity_hash` written on insert; future read endpoint can recompute and assert it matches (any DB tampering surfaces).
- We do NOT add a DB-level trigger blocking UPDATE — the existing admin DB connection has full rights, and the API is the trust boundary. A trigger would prevent legitimate schema migrations.

---

## Phased Breakdown

> Each phase ends with a HARD STOP for review. Do not start the next phase until the user confirms.

---

### Phase 1: Settings extension + snapshot schema

**Goal:** DB has a `nisab_grams` column and a `zakat_snapshots` table. Settings API exposes nisab. No screen, no compute logic yet.

**Files:**
- Modify: `app/models/__init__.py` (add `nisab_grams` to `Settings`, add `ZakatSnapshot` class)
- Modify: `app/schemas/settings.py` (add `nisab_grams` to `SettingsOut` + `SettingsUpdate`)
- Create: `alembic/versions/<hash>_zakat_nisab_and_snapshots.py`

**Tasks:**

- [ ] **1.1 Add `nisab_grams` to `Settings` model** ([app/models/__init__.py:273](../../app/models/__init__.py#L273)). Insert after `gold_refresh_minutes`:
  ```python
  nisab_grams: Mapped[Decimal] = mapped_column(
      Numeric(10, 3), nullable=False, default=Decimal("85.000")
  )
  ```

- [ ] **1.2 Add `ZakatSnapshot` model** at end of `app/models/__init__.py`. Use exact definition from "Data Model Changes" section above.

- [ ] **1.3 Add fields to `SettingsOut`** ([app/schemas/settings.py:7](../../app/schemas/settings.py#L7)):
  ```python
  nisab_grams: Decimal
  ```

- [ ] **1.4 Add field to `SettingsUpdate`** with `Field(gt=0)` validation:
  ```python
  nisab_grams: Decimal | None = Field(default=None, gt=0)
  ```

- [ ] **1.5 Generate Alembic migration:**
  ```
  cd jewelry_backend && alembic revision --autogenerate -m "zakat: add nisab to settings and zakat_snapshots table"
  ```
  Review the generated file — confirm it adds the column (with `server_default='85.000'` so existing rows backfill) and creates the table with all indexes.

- [ ] **1.6 Run migration:** `alembic upgrade head`. Confirm settings row has `nisab_grams = 85.000`.

- [ ] **1.7 Smoke test settings API:**
  ```bash
  curl -s -b cookies.txt http://localhost:8000/api/settings | jq .nisab_grams
  # expect "85.000"
  curl -s -b cookies.txt -X PATCH http://localhost:8000/api/settings \
    -H 'Content-Type: application/json' -d '{"nisab_grams":"87.500"}' | jq .nisab_grams
  # expect "87.500"
  ```

- [ ] **1.8 Commit:** `git add -A && git commit -m "feat(zakat): add nisab setting and snapshot table"`

**Definition of done:** Migration runs forward and back cleanly. Settings GET returns `nisab_grams`. PATCH validates `> 0`. `zakat_snapshots` table exists with all indexes.

**🛑 STOP for review.**

---

### Phase 2: Compute engine (the isolation point)

**Goal:** A pure, well-tested service that computes the zakat summary. No API, no screen — engine only.

**Files:**
- Create: `app/core/zakat.py`
- Create: `tests/test_zakat_compute.py`

**Tasks:**

- [ ] **2.1 Define dataclasses** at top of `app/core/zakat.py`:
  ```python
  from dataclasses import dataclass, field
  from decimal import Decimal
  from app.models import Karat

  @dataclass
  class KaratBucket:
      karat: Karat
      grams_by_source: dict[str, Decimal]  # keys: 'products','coins','ounces','lots'
      total_weight_grams: Decimal
      au_grams: Decimal

  @dataclass
  class ZakatHoldings:
      by_karat: list[KaratBucket]
      total_au_grams: Decimal

  @dataclass
  class ZakatSummary:
      holdings: ZakatHoldings
      gold_rate_24k: Decimal
      gold_rate_source: str
      gold_rate_is_stale: bool
      gold_rate_fetched_at: datetime
      nisab_grams: Decimal
      meets_nisab: bool
      total_au_value_usd: Decimal
      zakat_au_grams: Decimal
      zakat_value_usd: Decimal
  ```

- [ ] **2.2 Write failing test** `tests/test_zakat_compute.py::test_compute_zakatable_holdings_sums_all_four_types`. Seed: 1 K21 product (10g), 1 K22 coin type (5g × 4 on hand = 20g), 1 K24 ounce type (31.104g × 1 = 31.104g), 1 K18 lot (50g remaining). Assert per-karat Au totals and grand total.

- [ ] **2.3 Run test — verify it fails** with import / function-missing error.

- [ ] **2.4 Implement `compute_zakatable_holdings(db)`** in `app/core/zakat.py`. Four queries (one per type), filtered as documented in the table above. Group by karat. Use `KARAT_PURITY` from `app.core.pricing`. Decimal math only. Add module docstring marking this as the GROSS-vs-NET switchpoint.

  **Add this exact code comment above the products query:**
  ```python
  # NOTE: RESERVED is included because ProductStatus.RESERVED is currently a
  # placeholder — no code path writes it today (verified 2026-05-24). If/when a
  # reserve flow is added (e.g. "paid, awaiting pickup"), reconsider whether
  # those items still belong in zakatable holdings — they may have transferred
  # ownership economically even if still physically present.
  ```

- [ ] **2.5 Run test — verify it passes.**

- [ ] **2.6 Add edge-case tests:**
  - Empty inventory → returns empty `by_karat`, `total_au_grams == 0`.
  - Sold/melted products excluded.
  - Depleted lots excluded.
  - Coins with `on_hand_qty == 0` excluded.

- [ ] **2.7 Write failing test** `test_compute_zakat_summary_uses_live_rate_and_nisab`. Mock `get_current_gold_rate` to return `{"rate": 100.00, "source": "live", "fetched_at": ..., "is_stale": False}`. Seed inventory with `total_au_grams = 100.000`. Assert: `total_au_value_usd == 10_000.00`, `zakat_au_grams == 2.500`, `zakat_value_usd == 250.00`, `meets_nisab == True` if nisab is 85.

- [ ] **2.8 Implement `compute_zakat_summary(db)`** that calls `compute_zakatable_holdings`, reads gold rate, reads `Settings.nisab_grams`, applies 2.5%, rounds cash at the response boundary.

- [ ] **2.9 Run summary tests — verify pass.**

- [ ] **2.10 Edge-case tests for summary:** stale rate flag surfaces; `get_current_gold_rate` raising propagates as `RuntimeError`; nisab not met flips `meets_nisab` to False; nisab missing → defaults to 0 only if the migration default failed (shouldn't happen — assert presence).

- [ ] **2.11 Commit:** `git add -A && git commit -m "feat(zakat): compute service with full karat breakdown and nisab"`

**Definition of done:** All tests pass. The compute module imports nothing from API/schema layers. The GROSS-vs-NET comment is present and explicit.

**🛑 STOP for review.**

---

### Phase 3: Live endpoint

**Goal:** `GET /api/zakat` returns the live summary. Admin-only.

**Files:**
- Create: `app/schemas/zakat.py`
- Create: `app/api/zakat.py`
- Modify: `app/main.py` (register router)
- Add: `tests/test_zakat_api.py::test_live_endpoint_*`

**Tasks:**

- [ ] **3.1 Define Pydantic response schemas** in `app/schemas/zakat.py`: `KaratBucketOut`, `ZakatHoldingsOut`, `ZakatSummaryOut`. Mirror the dataclasses; use `Decimal` types.

- [ ] **3.2 Write failing endpoint test:** as admin, `GET /api/zakat` returns 200 with expected shape. As cashier, returns 403. With no gold rate available, returns 503.

- [ ] **3.3 Create router** `app/api/zakat.py`:
  ```python
  router = APIRouter(prefix="/zakat", tags=["zakat"], dependencies=[Depends(require_admin)])

  @router.get("", response_model=ZakatSummaryOut)
  async def live_zakat(db: AsyncSession = Depends(get_db)):
      try:
          summary = await compute_zakat_summary(db)
      except RuntimeError as e:
          raise HTTPException(503, detail=str(e))
      return _to_out(summary)
  ```

- [ ] **3.4 Register router** in `app/main.py` — add `zakat.router` to the include loop alongside the others.

- [ ] **3.5 Run tests — verify pass.**

- [ ] **3.6 Manual smoke (uvicorn running):**
  ```bash
  curl -s -b cookies.txt http://localhost:8000/api/zakat | jq .
  ```
  Expect a full summary with per-karat breakdown.

- [ ] **3.7 Commit:** `git add -A && git commit -m "feat(zakat): live summary endpoint"`

**Definition of done:** Endpoint returns full summary for admin, 403 for cashier, 503 on missing rate. Shape matches Pydantic schema exactly.

**🛑 STOP for review.**

---

### Phase 4: Snapshot endpoints (immutable)

**Goal:** Admin can persist a dated snapshot; list and fetch by ID; cannot mutate or delete.

**Files:**
- Modify: `app/api/zakat.py`
- Modify: `app/schemas/zakat.py` (add snapshot create/out schemas)
- Add: `tests/test_zakat_api.py::test_snapshot_*`

**Tasks:**

- [ ] **4.1 Add schemas:** `ZakatSnapshotCreate(assessment_date: date, notes: str | None)`, `ZakatSnapshotOut(id, taken_at, assessment_date, taken_by_user_id, notes, gold_rate_24k_usd_per_gram, gold_rate_source, nisab_grams_used, total_au_grams, total_au_value_usd, zakat_au_grams, zakat_value_usd, meets_nisab, breakdown_by_karat, integrity_hash)`, `ZakatSnapshotListOut(items: list[ZakatSnapshotOut], total: int)`.

- [ ] **4.2 Add helper** in `app/core/zakat.py`: `compute_integrity_hash(snapshot_fields: dict) -> str` — sha256 over a canonical JSON dump of all inputs + outputs. Pure function, separately testable.

- [ ] **4.3 Write failing test:** `POST /api/zakat/snapshots` with body `{"assessment_date":"2026-05-24","notes":"Q1"}` returns 201 with full snapshot; `GET /api/zakat/snapshots` lists it; `GET /api/zakat/snapshots/{id}` returns it; `PATCH` and `DELETE` are not registered (404 or 405).

- [ ] **4.4 Implement `POST /zakat/snapshots`** — re-compute summary inside a single `async with db.begin():` block, persist `ZakatSnapshot`, write integrity hash, record an `InventoryLedger` event of type `"zakat_snapshot_created"` for cross-trail audit.

- [ ] **4.5 Implement `GET /zakat/snapshots`** with paging (`page`, `page_size`) and optional date range filter, ordered by `taken_at DESC`.

- [ ] **4.6 Implement `GET /zakat/snapshots/{id}`** — 404 if not found. Recompute integrity_hash on read and include `integrity_ok: bool` in the response.

- [ ] **4.7 Run tests — verify pass.**

- [ ] **4.8 Smoke:**
  ```bash
  curl -s -b cookies.txt -X POST http://localhost:8000/api/zakat/snapshots \
    -H 'Content-Type: application/json' -d '{"assessment_date":"2026-05-24"}'
  curl -s -b cookies.txt http://localhost:8000/api/zakat/snapshots | jq '.items | length'
  ```

- [ ] **4.9 Commit:** `git add -A && git commit -m "feat(zakat): immutable snapshot endpoints with integrity hash"`

**Definition of done:** Snapshot persists all required fields. No mutation endpoints exist. Integrity hash recomputable on read.

**🛑 STOP for review.**

---

### Phase 5: Frontend Settings — nisab input

**Goal:** Admin can edit `nisab_grams` from the existing Settings page.

**Files:**
- Modify: `src/types/api.ts`
- Modify: `src/app/admin/settings/page.tsx`

**Tasks:**

- [ ] **5.1 Add `nisab_grams: number | string;`** to the `Settings` interface in `src/types/api.ts` after `gold_refresh_minutes`.

- [ ] **5.2 Add input** to the Default Pricing tab in `src/app/admin/settings/page.tsx`. Place it in a new sub-section "Zakat" under Default Pricing, following the existing input pattern (label + numeric input + onChange writing to `form`). Helper text: "Threshold (in grams of pure gold) above which zakat is due. Conventionally ~85g."

- [ ] **5.3 Smoke in browser:** edit nisab from 85 to 87.5, save, refresh, value persists.

- [ ] **5.4 Commit:** `git add -A && git commit -m "feat(zakat): nisab setting in admin UI"`

**Definition of done:** Nisab editable, persists, type-check clean.

**🛑 STOP for review.**

---

### Phase 6: Frontend Zakat tab + page

**Goal:** First-class sidebar tab `/admin/zakat` that fetches the live summary, shows breakdown + snapshots, lets admin save a snapshot.

**Files:**
- Create: `src/app/admin/zakat/page.tsx`
- Modify: `src/app/admin/layout.tsx` (add NAV entry)
- Modify: `src/i18n/en.ts` and `src/i18n/ar.ts` (add `nav.zakat` + `zakat: {...}` section)
- Create: `src/types/zakat.ts` (or extend `src/types/api.ts`)

**Tasks:**

- [ ] **6.1 Add types** in `src/types/zakat.ts` mirroring the backend Pydantic schemas.

- [ ] **6.2 Add i18n keys** to `en.ts`:
  - Interface: add `zakat: string` to `nav` interface; add a top-level `zakat: { title, totalAu, cashValue, zakatDue, perKarat, weightTotal, auEquivalent, nisab, meetsNisab, belowNisab, snapshot, snapshotBtn, snapshotsHistory, takenAt, assessmentDate, source, stale, latestPerDate, allSnapshots, ... }: { ... }` to the root interface.
  - Fill English values inline.
  - **Arabic:** ask the user to supply zakat terms (e.g. زكاة, نصاب, etc.). Wire up structure with placeholder Arabic strings, then commit a stub like `// TODO(ar): user to provide zakat terminology — see plan §6.2`. Replace in a follow-up commit before merge.

- [ ] **6.3 Add NAV entry** in `src/app/admin/layout.tsx` line 24-35 — slot between "Gold Price" and "Settings":
  ```ts
  { href: "/admin/zakat", icon: Scale, label: t.nav.zakat },
  ```
  Import `Scale` from lucide-react alongside the existing icons.

- [ ] **6.4 Create page** `src/app/admin/zakat/page.tsx`:
  - SWR fetch `/zakat` and `/zakat/snapshots`.
  - Top card: total Au grams (3dp), total cash USD (2dp), gold rate used (+ stale badge if applicable).
  - Card: zakat due — grams (3dp) and cash (2dp), with nisab comparison (✅ meets / ⚠️ below).
  - Table: per-karat breakdown with columns `Karat | Products (g) | Coins (g) | Ounces (g) | Lots (g) | Total (g) | Au (g)`. Footer row sums.
  - Snapshot section: "Save snapshot" button (opens modal with assessment date + optional notes) → `POST /zakat/snapshots` → `mutate()` both keys.
  - Snapshots history: table with columns `Assessment Date | Taken | Total Au (g) | Zakat (g) | Zakat (USD) | Rate | Source`. Row click opens detail (could be a future improvement; v1 just shows the table).

- [ ] **6.5 Smoke:**
  - Visit `/admin/zakat` as admin → page loads with correct totals.
  - As cashier → middleware redirects to `/pos`.
  - Edit nisab in Settings → returns to /admin/zakat → meets/below flips correctly.
  - Save snapshot → appears in history table.

- [ ] **6.6 Commit:** `git add -A && git commit -m "feat(zakat): admin sidebar tab with live summary and snapshots"`

**Definition of done:** New sidebar tab visible only to admins; page renders all required sections; snapshot flow works end-to-end.

**🛑 STOP for review. Feature complete.**

---

## Future Phase 7: Net-of-Debt (NOT in this plan)

When the religious ruling lands, the *only* change required is in `app/core/zakat.py::compute_zakatable_holdings()`. After computing the gross per-karat dict, query `SupplierBalance` filtered by `unit == DebtUnit.GOLD`, sum by `karat`, subtract from each bucket (clamping to `max(0, ...)`). Add a one-line `accounting_mode: Literal["gross", "net"]` to `ZakatSummary` so snapshots taken under either rule are distinguishable. No schema migration required — the existing `breakdown_by_karat` JSON column absorbs the extra metadata; only the snapshot's *interpretation* changes.

**Confirmation:** the API response shape, the snapshot table, the screen layout, and the sidebar all remain identical. The screen would gain at most a small mode toggle, fed from a new `Settings.zakat_accounting_mode` enum.

---

## Open Questions

All answered 2026-05-24 (see "Resolved Decisions" near the top). None remaining.

The snapshots list UI (Phase 6.4) shows **latest-per-date by default** with a toggle to reveal all snapshots:
```
[●] Latest per date    [ ] All snapshots
```
Implementation: SWR fetch all, group client-side by `assessment_date`, default render shows `max(taken_at)` per group; toggle renders flat.

---

## Self-Review Checklist

- [x] Every spec requirement traced to a task:
  - Tab in sidebar → Phase 6.3
  - Sums across all four types → Phase 2 + table above
  - Karat purity in ONE place → reuse `KARAT_PURITY` (Phase 2.4)
  - K22 included → enum already has it; purity map already has it
  - Total Au + cash value → Phase 2.8, Phase 6.4
  - Zakat in grams + cash → Phase 2.8, Phase 6.4
  - Per-karat breakdown by source → Phase 2 dataclass + Phase 6.4 table
  - Nisab configurable → Phase 1 (DB) + Phase 5 (UI)
  - LIVE recompute → Phase 3
  - Dated immutable snapshots → Phase 4
  - Gross now, net-of-debt later → isolated in `compute_zakatable_holdings`
- [x] No placeholders.
- [x] Type names consistent: `ZakatHoldings`, `ZakatSummary`, `KaratBucket`, `ZakatSnapshot`, `ZakatSummaryOut`, `ZakatSnapshotOut` used consistently across phases.
- [x] Each phase ends with a hard stop and is independently testable.
