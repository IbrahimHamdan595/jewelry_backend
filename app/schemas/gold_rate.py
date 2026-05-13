from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class GoldRateOut(BaseModel):
    rate_24k: float
    rate_21k: float
    rate_18k: float
    source: str
    fetched_at: datetime
    is_stale: bool


class GoldRateHistoryPoint(BaseModel):
    rate_24k: float
    fetched_at: datetime


class OverrideRequest(BaseModel):
    rate_24k: Decimal
