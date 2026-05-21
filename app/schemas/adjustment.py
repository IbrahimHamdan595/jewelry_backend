from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class AdjustmentCreate(BaseModel):
    target_type: str  # LOT (Phase 1); PRODUCT/COIN_STOCK/OUNCE_STOCK in later phases
    target_id: str
    delta: Decimal  # signed; grams for LOT
    reason: str  # LOSS | THEFT | GIFT | SAMPLE | CORRECTION
    notes: str = Field(min_length=1)


class AdjustmentOut(BaseModel):
    id: str
    target_type: str
    target_id: str
    delta: Decimal
    reason: str
    notes: str
    occurred_at: datetime
    actor_user_id: str
    created_at: datetime

    model_config = {"from_attributes": True}
