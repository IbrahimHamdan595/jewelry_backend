from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


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
    rate_24k: Decimal = Field(gt=0)
    # Audit phase A3: every manual override now carries a justification that
    # lands in the SETTINGS_CHANGED / GOLD_RATE_OVERRIDE_SET ledger payload.
    # Short minimum stops empty / whitespace-only submissions; cap is just
    # to keep the payload reasonable.
    reason: str = Field(min_length=3, max_length=500)
