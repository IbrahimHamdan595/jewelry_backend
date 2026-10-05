import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.core.rate_limit import limiter
from app.core.security import PasswordTooLongError
from app.jobs.gold_rate_poller import scheduler, start_gold_rate_poller
from app.api import (
    accounting, adjustments, ap, ar, auth, auth_audit, bank, buybacks, categories, coins,
    expenses, gold_price, inventory, ledger, lots, melts, orders, ounces, polish, products,
    reports, statements, stock_takes, suppliers, tax, zakat,
)
from app.api import settings as settings_router
from app.api import staff

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("CORS allowed origins: %s", settings.cors_origins)
    start_gold_rate_poller(interval_minutes=settings.gold_refresh_minutes)
    yield
    if scheduler.running:
        scheduler.shutdown()


origins = settings.cors_origins
# NEX-47: /docs, /redoc and /openapi.json publish the whole API surface to
# anyone, logged in or not, so they are only mounted outside production.
# ENVIRONMENT defaults to production — see app/config.py.
docs_enabled = not settings.is_production
app = FastAPI(
    title="Fawaz El Namel API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if docs_enabled else None,
    redoc_url="/redoc" if docs_enabled else None,
    openapi_url="/openapi.json" if docs_enabled else None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


async def _password_too_long_handler(request: Request, exc: PasswordTooLongError) -> JSONResponse:
    """A new password over bcrypt's 72 bytes is the caller's mistake, not ours.

    hash_password raises this from whichever route was setting a password
    (change-password, staff create, staff update); answered here once so all
    of them give the same 422 with a message the UI can show as it is.
    Nothing has been committed at that point — the request's session is
    simply discarded.
    """
    return JSONResponse(status_code=422, content={"detail": str(exc)})


app.add_exception_handler(PasswordTooLongError, _password_too_long_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", include_in_schema=False)
async def health():
    """Liveness probe for the container HEALTHCHECK / the platform.

    Deliberately shallow: no database, no upstream call. A probe that waits
    on the database turns one slow query into "unhealthy", a restart, and
    the same slow query again. This only says the process is serving HTTP.
    """
    return {"status": "ok"}


for r in (
    auth.router,
    products.router,
    orders.router,
    gold_price.router,
    settings_router.router,
    staff.router,
    reports.router,
    categories.router,
    lots.router,
    adjustments.router,
    ledger.router,
    coins.router,
    ounces.router,
    buybacks.router,
    suppliers.router,
    suppliers.ap_router,
    melts.router,
    polish.router,
    inventory.router,
    zakat.router,
    auth_audit.router,
    stock_takes.router,
    accounting.router,
    bank.router,
    ar.router,
    ap.router,
    expenses.router,
    tax.router,
    statements.router,
):
    app.include_router(r, prefix="/api")
