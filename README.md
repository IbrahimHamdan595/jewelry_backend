# Backend

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
- **JWT auth** via `python-jose` (HS256, or RS256 once a key pair is
  provisioned so the frontend only ever holds a public key), HttpOnly cookie
  set by the backend on login; bcrypt for password hashing. Login is throttled twice: SlowAPI
  rate-limit (5/min per client IP, taken from `X-Forwarded-For`) and a
  per-account lockout (10 consecutive failures in 15 min lock that email
  for 15 min, derived from the auth audit log). The lockout cuts both ways:
  it stops a guess spread over many addresses, and it lets anyone who knows
  an email keep its owner out for 15 minutes at a time. An admin lifts a
  lock at once with `POST /api/staff/{id}/unlock` (audited as
  `ACCOUNT_UNLOCKED`).
- **Sessions can be ended server-side.** Every token carries the user's
  `token_version` and `get_current_user` re-checks it on each request.
  Changing a password (own, or an admin reset) and
  `POST /api/staff/{id}/force-logout` bump it, which signs that user out
  everywhere; the person changing their own password gets a fresh cookie in
  the same response. Plain logout only drops the cookie on that device, so
  shop tills sharing one account do not log each other out.
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
# Environment — unset means "production", which is what hides /docs, /redoc
# and /openapi.json (NEX-47). Set development on your own machine to get the
# interactive docs back. Never set this on Render.
ENVIRONMENT=development

# Database
DATABASE_URL="postgresql+asyncpg://user:pass@host/dbname?ssl=require"

# Auth — JWT_SECRET MUST be a strong random string in any non-dev env
JWT_SECRET="..."
JWT_ALGORITHM="HS256"
JWT_EXPIRES_MINUTES=480
# RS256 signing — optional; see "JWT signing keys (RS256)" below. With none of
# these set, tokens are HS256 signed with JWT_SECRET.
# JWT_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----"
# JWT_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----"
# JWT_ACCEPT_HS256=true

# CORS — comma-separated list of allowed frontend origins. Exact origins only:
# there is deliberately no wildcard/regex (NEX-45). If you develop through a
# dev tunnel, add YOUR tunnel's frontend origin to your local .env, e.g.
# CORS_ORIGINS="http://localhost:3000,https://<your-tunnel>-3001.devtunnels.ms"
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
GOLD_REFRESH_MINUTES=10
GOLD_ALERT_FAILURE_THRESHOLD=2

# Cloudflare R2 (product image uploads)
R2_ACCOUNT_ID="..."
R2_ACCESS_KEY_ID="..."
R2_SECRET_ACCESS_KEY="..."
R2_BUCKET_NAME="..."
R2_PUBLIC_URL="https://pub-....r2.dev"

# Discord webhook (gold-rate poller alerts + reconcile drift alerts)
# GOLD_ALERT_FAILURE_THRESHOLD lives in the Gold rate block above — it gates the
# Discord alert AND the market_closed threshold. See "Gold rate staleness".
DISCORD_WEBHOOK_URL="..."
DISCORD_ALERT_USER_ID="..."

# Auth audit (default 540 days = 18 months)
AUTH_AUDIT_RETENTION_DAYS=540
```

**Never commit `.env`.** It's gitignored. In production (Render), inject
these as service-level env vars.

### Gold rate staleness

Two settings together control when the system starts warning about — and then
gating on — an old gold rate:

| Setting | Shop value | Effect |
|---|---|---|
| `GOLD_REFRESH_MINUTES` | `10` | Poll interval, **and** the `is_stale` threshold. |
| `GOLD_ALERT_FAILURE_THRESHOLD` | `2` | Consecutive poll failures before the Discord alert. |

Derived in `app/core/gold_api.py`:

    is_stale      after GOLD_REFRESH_MINUTES                       -> 10 min
    market_closed after max(stale x 2, FAILURE_THRESHOLD x stale)  -> 20 min

`market_closed` is not cosmetic: past that point `POST /api/orders` and
`POST /api/buybacks` return **409** unless the request carries a
`stale_rate_ack` naming the exact rate timestamp being accepted, which is then
written to the inventory ledger as `SALE_ON_STALE_RATE_ACK`
(`app/core/gold_guard.py`). An active admin override clears the gate entirely.

**Raising `GOLD_REFRESH_MINUTES` also delays the point at which the till starts
prompting.** They are the same knob.

### JWT signing keys (RS256)

The Next.js middleware verifies the session token itself. With HS256 that
means the frontend host holds `JWT_SECRET` — the same key that **mints**
tokens. With RS256 the backend keeps a private key and the frontend gets a
public key that can only verify (NEX-54).

| Setting | Default | Effect |
|---|---|---|
| `JWT_PRIVATE_KEY` | unset | When set, new tokens are signed **RS256** with it. Unset: HS256 with `JWT_SECRET`, as before. |
| `JWT_PUBLIC_KEY` | unset | Verifies RS256 tokens. Derived from the private key if left unset. The frontend needs this value. |
| `JWT_ACCEPT_HS256` | `true` | Keep accepting `JWT_SECRET`-signed tokens. Set `false` to end the migration window. |

Both keys are PEM. Newlines may be real or written as the two characters `\n`.
`JWT_ALGORITHM` is **not** how RS256 is selected — it only names the
shared-secret algorithm; leave it alone on the backend.

Merging this changes nothing until `JWT_PRIVATE_KEY` is set. If the keys are
unusable (not PEM, public pasted as private, a public key that does not belong
to the private one) the service refuses to start, so a bad key fails the deploy
instead of breaking every login.

**Generate a pair:**

```bash
# Private key (PKCS#8). Stays on the backend host; never goes to Vercel.
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out jwt_private.pem

# Public key (SPKI, "BEGIN PUBLIC KEY"). This is the half the frontend gets.
openssl rsa -in jwt_private.pem -pubout -out jwt_public.pem

# One-line forms, for env-var fields that do not take multi-line values:
awk 'NF {printf "%s\\n", $0}' jwt_private.pem; echo
awk 'NF {printf "%s\\n", $0}' jwt_public.pem; echo

# Once both are in Render / Vercel, do not keep them on disk or in git:
rm jwt_private.pem jwt_public.pem
```

**Cutover order** (do steps 2 and 3 back to back, outside shop hours):

1. Generate the pair.
2. **Render (backend):** set `JWT_PRIVATE_KEY` and `JWT_PUBLIC_KEY`. Leave
   `JWT_ACCEPT_HS256`, `JWT_SECRET` and `JWT_ALGORITHM` as they are. Deploy.
   New logins are RS256; sessions already open (HS256) keep working.
3. **Vercel (frontend):** set `JWT_PUBLIC_KEY` to the same public key and
   `JWT_ALGORITHM=RS256`. Redeploy. If the middleware checks one algorithm at
   a time, a login made between steps 2 and 3 bounces back to `/login` until
   this step lands, and anyone still on an HS256 cookie afterwards is asked
   to log in once.
4. **Wait `JWT_EXPIRES_MINUTES` (8 hours)** so every HS256 token has expired.
5. **Render:** set `JWT_ACCEPT_HS256=false`. From here `JWT_SECRET` can
   neither mint nor verify a session.
6. **Vercel:** remove `JWT_SECRET`. On Render `JWT_SECRET` must stay defined
   (the config requires it) — leave its value in place rather than blanking it.

Rollback before step 5: unset `JWT_PRIVATE_KEY` on Render (signing goes back to
HS256; keep `JWT_PUBLIC_KEY` so RS256 sessions already issued stay valid) and
restore `JWT_ALGORITHM=HS256` / remove `JWT_PUBLIC_KEY` on Vercel.

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
- Interactive docs: http://localhost:8000/docs — only with
  `ENVIRONMENT=development` in your `.env`. In production (the default) `/docs`,
  `/redoc` and `/openapi.json` return 404.

## Run with Docker

```bash
docker build -t fawaz-el-namel-backend .
docker run --rm -p 8000:8000 --env-file .env fawaz-el-namel-backend
```

The image runs as an unprivileged user and listens on `$PORT` (8000 when
unset). Its `HEALTHCHECK` polls `GET /health`, which only reports that the
process is serving — it never touches the database, so a slow query cannot
restart the container.

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
│   │   ├── staff.py            # cashier user management (audited) + unlock, force-logout
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
│   │   ├── login_lockout.py    # per-account lockout, derived from auth_audit_log
│   │   ├── notify.py           # Discord webhook
│   │   ├── permissions.py      # require_admin
│   │   ├── pricing.py          # KARAT_PURITY, calculate_price, etc.
│   │   ├── rate_limit.py       # SlowAPI limiter (login), keyed on the client IP
│   │   ├── security.py         # JWT + bcrypt + revoke_sessions (token_version)
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
- **Health check path:** `/health` (no auth, no database access).
- **API docs:** leave `ENVIRONMENT` unset (or `production`). Any value other
  than `development` / `dev` / `local` / `test` keeps `/docs`, `/redoc` and
  `/openapi.json` at 404.
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
