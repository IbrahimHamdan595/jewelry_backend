# MAISON ZAHAB — Backend

FastAPI backend for a gold-jewellery point-of-sale + inventory + procurement
system. Tracks four kinds of gold stock (atomic products, coin types, ounce
bars, pure-gold lots), POS sales, walk-in buybacks, supplier purchases with
AP balances, gold-rate polling, zakat computation, and a tamper-evident
audit trail.

Paired with [`jewelry_frontend`](https://github.com/IbrahimHamdan595/jewelry_frontend)
(Next.js admin + POS UI).

---

## What this system does

| Area | What's in it |
|---|---|
| **POS** | Cashier UI for selling products, coins, and ounce bars. Live gold-rate-driven pricing with per-karat purity (K18 / K21 / K22 / K24), making charges, VAT, LBP conversion. Receipt printing. |
| **Walk-in buybacks** | Shop buys gold back from a customer — pure gold (→ lot), coin/ounce stock (→ on-hand qty), or a used product (→ inventory). Configurable buyback margin (per-gram or %). |
| **Inventory** | Four parallel inventory types with their own mutation paths: `Product` (atomic items with status AVAILABLE/SOLD/MELTED/RESERVED/INACTIVE), `CoinType` / `OunceType` (qty-based with `on_hand_qty`), `GoldLot` (raw pure gold with `weight_remaining_grams`). |
| **Suppliers & AP** | Record supplier purchases (cash, gold, or mixed), record payments, track per-karat gram debt and cash debt per supplier. Reconciliation endpoint replays purchases minus payments. |
| **Melt / polish** | Convert products or used-product buybacks into pure-gold lots; polish lots back into product inventory. |
| **Gold rate** | Polled from GoldAPI (primary) with a goldprice.org public-feed fallback (called "LBMA" in code). Manual override supported, with required reason and full audit. |
| **Zakat** | Computes total pure Au across all four inventory types, applies 2.5%, compares to a configurable nisab. Immutable dated snapshots with SHA-256 integrity hash. See [`docs/superpowers/plans/2026-05-24-zakat-and-pure-gold.md`](docs/superpowers/plans/2026-05-24-zakat-and-pure-gold.md). |
| **Audit trail** | Every state mutation writes to a hash-chained `inventory_ledger`. Auth events (login success/failure, logout, password change) write to a separate hash-chained `auth_audit_log`. DB-level triggers block UPDATE/DELETE on the audit tables. Physical stock-take workflow with variance approval through the same audited mutation path. **See [`docs/AUDIT_CONTROLS.md`](docs/AUDIT_CONTROLS.md) for the full controls reference.** |

---

## Architecture at a glance

- **FastAPI** + **SQLAlchemy 2.x async** + **PostgreSQL** (Neon-hosted in
  prod; in-memory SQLite via aiosqlite for tests).
- **Alembic** for every schema change. Never `create_all` in production.
- **APScheduler** for the gold-rate poller (runs every N minutes, alerts
  via Discord webhook after N consecutive failures).
- **JWT auth** via `python-jose`, HttpOnly cookie set by the backend on
  login; bcrypt for password hashing; SlowAPI rate-limit on login
  (5/min/IP).
- **Cloudflare R2** for product image uploads.
- **Two hash chains** for audit integrity: one for inventory events, one
  for auth events. Each row contains
  `entry_hash = sha256(canonical(fields) || prev_hash)`. Editing or
  deleting any row breaks the chain at that exact row, detectable via
  `GET /api/ledger/verify` and `GET /api/auth-audit/verify`.

---

## Requirements

- Python 3.11+ (3.12 also fine; production currently runs 3.11)
- PostgreSQL 14+ in production (asyncpg driver). SQLite + aiosqlite is
  used by the test fixture — no Postgres needed to run the test suite.

## Setup

From the `jewelry_backend/` directory:

```bash
# 1. Create and activate a virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies (includes pytest, pytest-asyncio, aiosqlite for tests)
pip install -r requirements.txt
```

## Environment

Create a `.env` file in `jewelry_backend/`. The full schema lives in
[`app/config.py`](app/config.py); the practical minimum is:

```ini
# Database
DATABASE_URL="postgresql+asyncpg://user:pass@host/dbname?ssl=require"

# Auth — JWT_SECRET MUST be a strong random string in any non-dev env
JWT_SECRET="..."
JWT_ALGORITHM="HS256"
JWT_EXPIRES_MINUTES=480

# CORS — comma-separated list of allowed frontend origins
CORS_ORIGINS="http://localhost:3000,https://your-frontend.example.com"

# Cookie flags — for HTTPS cross-origin (Render etc.) set: true / none
COOKIE_SECURE=false
COOKIE_SAMESITE=lax

# Seed admin (used only by `python -m app.seed`)
SEED_ADMIN_EMAIL="owner@example.com"
SEED_ADMIN_PASSWORD="..."

# Gold rate
GOLD_API_KEY="..."
GOLD_API_URL="https://www.goldapi.io/api/XAU/USD"
GOLD_REFRESH_MINUTES=15

# Cloudflare R2 (product image uploads)
R2_ACCOUNT_ID="..."
R2_ACCESS_KEY_ID="..."
R2_SECRET_ACCESS_KEY="..."
R2_BUCKET_NAME="..."
R2_PUBLIC_URL="https://pub-....r2.dev"

# Discord webhook (gold-rate poller alerts + reconcile drift alerts)
DISCORD_WEBHOOK_URL="..."
DISCORD_ALERT_USER_ID="..."
GOLD_ALERT_FAILURE_THRESHOLD=3

# Auth audit (default 540 days = 18 months)
AUTH_AUDIT_RETENTION_DAYS=540
```

**Never commit `.env`.** It's gitignored. In production (Render), inject
these as service-level env vars.

## Database migrations

Every schema change is an Alembic migration in `alembic/versions/`. Run
before starting the server (and after pulling new commits that include
migrations):

```bash
alembic upgrade head
```

To seed the initial admin user from `SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD`:

```bash
python -m app.seed
```

## Run the server

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

- API: http://localhost:8000
- Interactive docs: http://localhost:8000/docs

## Run with Docker

```bash
docker build -t maison-zahab-backend .
docker run --rm -p 8000:8000 --env-file .env maison-zahab-backend
```

---

## Tests

```bash
pytest -q
```

92 tests as of the latest audit-hardening sweep. Coverage areas:

- Pure pricing math (sale, unit, buyback)
- Zakat aggregator + integrity hash + filter correctness
- Hash chain semantics (determinism, tamper detection at exact row,
  N>1-writes-per-tx contiguity, timezone canonicalization)
- Auth audit chain (NULL-field tolerance, XFF parsing, best-effort
  failure handling, raise-after-fire regression)
- `field_diff` for settings/staff audit payloads
- Coin/ounce stock reconcile arithmetic (void-vs-refund correctness,
  multi-type isolation)
- Stock-take state machine (happy path, no-variance auto-close, reject,
  double-approve 409, expected-qty freeze across concurrent changes,
  status guards, rejected-drift-stays-visible)
- `StockTakeRefType → AdjustmentTarget` mapping completeness

Tests use an in-memory SQLite fixture ([`tests/conftest.py`](tests/conftest.py))
— no external services required.

---

## Repository layout

```
jewelry_backend/
├── app/
│   ├── api/                    # FastAPI routers, one per resource
│   │   ├── auth.py             # login / logout / change-password
│   │   ├── auth_audit.py       # GET /auth-audit + /verify (admin)
│   │   ├── adjustments.py      # MANUAL_ADJUSTMENT + apply_unit_stock_adjustment_core
│   │   ├── buybacks.py         # walk-in buybacks (4 kinds)
│   │   ├── categories.py
│   │   ├── coins.py            # CoinType CRUD
│   │   ├── gold_price.py       # rate read + override (audited) + refresh
│   │   ├── inventory.py        # alerts + supplier reconcile + reconcile-units
│   │   ├── ledger.py           # GET /ledger + /verify (admin)
│   │   ├── lots.py             # GoldLot CRUD
│   │   ├── melts.py            # product/used-buyback → lot
│   │   ├── orders.py           # POS sales + voids + refunds
│   │   ├── ounces.py
│   │   ├── polish.py           # lot → product
│   │   ├── products.py
│   │   ├── reports.py          # dashboard aggregates
│   │   ├── settings.py
│   │   ├── staff.py            # cashier user management (audited)
│   │   ├── stock_takes.py      # physical-count workflow (audit B2)
│   │   ├── suppliers.py        # suppliers + purchases + payments
│   │   └── zakat.py            # live computation + snapshots
│   │
│   ├── core/                   # Domain logic / cross-cutting helpers
│   │   ├── audit_chain.py      # hash chain (inventory + auth siblings)
│   │   ├── audit_maintenance.py  # A2 maintenance-flag bypass helper
│   │   ├── auth_audit.py       # best-effort recorder, get_client_ip
│   │   ├── cloudflare.py       # R2 image upload
│   │   ├── gold_api.py         # rate fetcher + override/history reader
│   │   ├── ledger.py           # record() + field_diff() + event types
│   │   ├── notify.py           # Discord webhook
│   │   ├── permissions.py      # require_admin
│   │   ├── pricing.py          # KARAT_PURITY, calculate_price, etc.
│   │   ├── rate_limit.py       # SlowAPI limiter (login)
│   │   ├── security.py         # JWT + bcrypt
│   │   ├── stock_take.py       # StockTakeRefType → AdjustmentTarget mapping
│   │   └── zakat.py            # holdings aggregator + integrity hash
│   │
│   ├── db/
│   │   ├── base.py             # SQLAlchemy DeclarativeBase
│   │   └── session.py          # async engine + sessionmaker
│   │
│   ├── jobs/
│   │   └── gold_rate_poller.py  # APScheduler job + alerting
│   │
│   ├── models/__init__.py      # ALL ORM models in one file
│   ├── schemas/                # Pydantic request/response models
│   ├── config.py               # Settings (pydantic-settings)
│   ├── deps.py                 # get_db, get_current_user, AUTH_COOKIE_NAME
│   ├── main.py                 # FastAPI app, CORS, lifespan, router includes
│   └── seed.py                 # initial admin + settings singleton
│
├── alembic/
│   ├── env.py
│   └── versions/               # Every schema change, append-only history
│
├── tests/                      # 92 tests; SQLite fixture in conftest.py
│
├── docs/
│   ├── AUDIT_CONTROLS.md       # ★ Single-page reference for all audit controls
│   └── superpowers/plans/      # Feature plans (zakat, B1/B2 reconcile+stock-take)
│
├── AUDIT_READINESS.md          # Original audit-hardening assessment + role-split recipe
├── alembic.ini
├── requirements.txt            # Runtime + dev/test dependencies
└── runtime.txt                 # Python version pin for Render
```

---

## Production deploy notes (Render)

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
  (Render injects `$PORT` — do not hardcode.)
- **Python:** pinned via `runtime.txt`.
- **CORS:** set `CORS_ORIGINS` to the exact frontend Render URL.
- **Cookies:** in production, set `COOKIE_SECURE=true` and
  `COOKIE_SAMESITE=none` since the frontend is on a different subdomain.
- **DB-role split:** `gold_app` runtime role separation is documented in
  [`docs/AUDIT_CONTROLS.md`](docs/AUDIT_CONTROLS.md#6-role-split-follow-up--exact-recipe).
  ~5 minutes in the Neon console; the audit triggers already block
  mutations, this is defense-in-depth.

---

## Documentation

| Doc | Read when… |
|---|---|
| **[`docs/AUDIT_CONTROLS.md`](docs/AUDIT_CONTROLS.md)** | You need the full picture of what audit controls exist, the invariants, the API surface, how to verify a chain, how to interpret a "broken" result. **Start here for anything audit-related.** |
| [`AUDIT_READINESS.md`](AUDIT_READINESS.md) | You want the original assessment of audit gaps that led to A1 → B2, plus the role-split follow-up. |
| [`docs/superpowers/plans/2026-05-24-zakat-and-pure-gold.md`](docs/superpowers/plans/2026-05-24-zakat-and-pure-gold.md) | You're working on zakat logic and want the design rationale. |
| [`docs/superpowers/plans/2026-05-25-b1-b2-reconcile-stock-take.md`](docs/superpowers/plans/2026-05-25-b1-b2-reconcile-stock-take.md) | You're working on stock reconcile or stock-take and want the design rationale, including the void-vs-refund analysis and the close-race fix. |
| Module-level docstrings in `app/core/audit_chain.py`, `app/core/audit_maintenance.py`, `app/core/auth_audit.py`, `app/core/ledger.py`, `app/api/stock_takes.py` | You're touching that specific module and want to understand its invariants. |

---

## License & ownership

Private project. All rights reserved by the project owner.
