import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.jobs.gold_rate_poller import scheduler, start_gold_rate_poller
from app.api import auth, categories, gold_price, orders, products, reports
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
    title="MAISON ZAHAB API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=r"https://.*\.devtunnels\.ms",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (auth.router, products.router, orders.router, gold_price.router, settings_router.router, staff.router, reports.router, categories.router):
    app.include_router(r, prefix="/api")
