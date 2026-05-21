from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class LotCreate(BaseModel):
    """Admin-initiated lot creation. Source restricted to SEED/ADJUSTMENT.

    Lots originating from buybacks, supplier purchases, or melts are created
    by their respective endpoints in later phases — not here.
    """
    karat: str
    weight_grams: Decimal = Field(gt=0)
    source: str  # SEED or ADJUSTMENT only
    cost_basis_usd: Decimal = Field(ge=0, default=Decimal("0"))
    acquired_at: datetime | None = None
    notes: str | None = None


class LotUpdate(BaseModel):
    """Only notes are mutable via API. Weight changes go through adjustments."""
    notes: str | None = None


class LotOut(BaseModel):
    id: str
    karat: str
    weight_grams: Decimal
    weight_remaining_grams: Decimal
    source: str
    source_ref_type: str | None
    source_ref_id: str | None
    cost_basis_usd: Decimal
    acquired_at: datetime
    notes: str | None
    is_depleted: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class LotListOut(BaseModel):
    items: list[LotOut]
    total: int
    page: int
    page_size: int


class LotKaratTotal(BaseModel):
    karat: str
    total_remaining_grams: Decimal
    total_original_grams: Decimal
    lot_count: int
    cost_basis_remaining_usd: Decimal


class LotTotalsOut(BaseModel):
    by_karat: list[LotKaratTotal]
    grand_total_remaining_grams: Decimal
