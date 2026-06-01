import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.core.rate_limit import limiter
from app.jobs.gold_rate_poller import scheduler, start_gold_rate_poller
from app.api import (
    adjustments, auth, auth_audit, buybacks, categories, coins, gold_price, inventory,
    ledger, lots, melts, orders, ounces, polish, products, reports, stock_takes,
    suppliers, zakat,
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
app = FastAPI(
    title="Fawaz El Namel API",
    version="1.0.0",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=r"https://.*\.devtunnels\.ms",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
):
    app.include_router(r, prefix="/api")
