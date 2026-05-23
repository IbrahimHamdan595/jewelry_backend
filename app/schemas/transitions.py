"""Schemas for Phase 6 transition endpoints: melt and polish."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.schemas.lot import LotOut
from app.schemas.product import ProductOut


class MeltCreate(BaseModel):
    """Melt a product OR a pending USED_PRODUCT buyback into a new pure-gold lot.

    Exactly one of `product_id` / `walkin_buyback_id` must be set.

    For PRODUCT melts: karat, weight, and cost basis come from the Product row.
    For USED_PRODUCT buyback melts: karat and weight come from the buyback;
    cost basis = buyback.buy_price_usd.

    Optional `override_*` fields let an admin record a different karat/weight
    discovered during the melt (the piece weighed differently on the scale,
    or tested as a different karat). The original values are preserved on
    the source row; only the new lot reflects the corrected figures.
    """
    product_id: str | None = None
    walkin_buyback_id: str | None = None
    override_weight_grams: Decimal | None = Field(default=None, gt=0)
    override_karat: str | None = None
    notes: str | None = None


class MeltOut(BaseModel):
    id: str
    occurred_at: datetime
    source_type: str  # "product" | "walkin_buyback"
    source_id: str
    lot: LotOut


class PolishCreate(BaseModel):
    """Polish a USED_PRODUCT buyback into a saleable Product.

    The buyback supplies: karat, weight_grams, cost_basis_usd (= buy_price_usd).
    The admin supplies the product-display fields (name, category, margin,
    making charge, photos) and any overrides on weight/karat after polishing.
    """
    walkin_buyback_id: str
    name_en: str = Field(min_length=1)
    name_ar: str = ""
    category: str = Field(min_length=1)
    category_id: str | None = None
    margin_percent: Decimal = Field(ge=0)
    making_charge: Decimal = Field(ge=0)
    photos: list[dict] = []
    override_weight_grams: Decimal | None = Field(default=None, gt=0)
    override_karat: str | None = None
    notes: str | None = None


class PolishOut(BaseModel):
    walkin_buyback_id: str
    product: ProductOut
