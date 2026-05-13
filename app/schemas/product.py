from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class ProductCreate(BaseModel):
    name_en: str
    name_ar: str = ""
    category: str
    category_id: str | None = None
    karat: str
    weight_grams: Decimal
    margin_percent: Decimal
    making_charge: Decimal
    photos: list[dict] = []


class ProductUpdate(BaseModel):
    name_en: str | None = None
    name_ar: str | None = None
    category: str | None = None
    category_id: str | None = None
    karat: str | None = None
    weight_grams: Decimal | None = None
    margin_percent: Decimal | None = None
    making_charge: Decimal | None = None
    photos: list[dict] | None = None
    is_active: bool | None = None


class ProductOut(BaseModel):
    id: str
    code: str
    name_en: str
    name_ar: str
    category: str
    category_id: str | None
    karat: str
    weight_grams: Decimal
    margin_percent: Decimal
    making_charge: Decimal
    photos: list[dict]
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProductLookupOut(BaseModel):
    id: str
    code: str
    name_en: str
    name_ar: str
    karat: str
    weight_grams: Decimal
    margin_percent: Decimal
    making_charge: Decimal
    gold_rate_24k: float
    purity_rate: Decimal
    final_price: Decimal

    model_config = {"from_attributes": True}


class ProductListOut(BaseModel):
    items: list[ProductOut]
    total: int
    page: int
    page_size: int
