"""Schemas for coin_types and ounce_types (shape-identical SKU catalogs)."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class UnitTypeCreate(BaseModel):
    # Optional: when omitted, the router auto-generates FN-COIN/OZ-{karat}-NNNN.
    code: str | None = None
    name_en: str = Field(min_length=1)
    name_ar: str = ""
    karat: str
    weight_grams: Decimal = Field(gt=0)
    markup_per_gram: Decimal = Decimal("0")
    margin_mode: str  # USD | PERCENT
    margin_value: Decimal = Field(ge=0, default=Decimal("0"))
    min_stock_qty: int | None = Field(default=None, ge=0)
    photo_url: str | None = None


class UnitTypeUpdate(BaseModel):
    name_en: str | None = None
    name_ar: str | None = None
    karat: str | None = None
    weight_grams: Decimal | None = None
    markup_per_gram: Decimal | None = None
    margin_mode: str | None = None
    margin_value: Decimal | None = None
    min_stock_qty: int | None = None
    photo_url: str | None = None
    is_active: bool | None = None


class UnitTypeOut(BaseModel):
    id: str
    code: str
    name_en: str
    name_ar: str
    karat: str
    weight_grams: Decimal
    markup_per_gram: Decimal
    margin_mode: str
    margin_value: Decimal
    on_hand_qty: int
    min_stock_qty: int | None
    photo_url: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class UnitTypeListOut(BaseModel):
    items: list[UnitTypeOut]
    total: int
    page: int
    page_size: int


class UnitPriceOut(BaseModel):
    type_id: str
    code: str
    gold_rate_24k: float
    effective_rate: Decimal
    metal_value: Decimal
    margin_amount: Decimal
    final_price: Decimal
    on_hand_qty: int
    rate_source: str
    rate_is_stale: bool
