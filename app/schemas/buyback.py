from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class BuybackQuoteOut(BaseModel):
    """Live quote for a pure-gold walk-in. Cashier shows this on screen."""
    rate_24k: Decimal
    rate_source: str
    rate_is_stale: bool
    karat: str
    purity_rate: Decimal
    weight_grams: Decimal
    margin_mode: str
    margin_value: Decimal
    effective_rate_per_gram: Decimal
    buy_price: Decimal


class BuybackCreate(BaseModel):
    """Single endpoint, kind discriminator drives the required fields.

    Required by kind:
      PURE_GOLD     → karat, weight_grams
      COIN          → coin_type_id, quantity
      OUNCE         → ounce_type_id, quantity
      USED_PRODUCT  → karat, weight_grams, manual_price (no product is created in
                      Phase 3; the polish step in Phase 6 will reference this row)
    """
    seller_name: str = Field(min_length=1)
    seller_phone: str = Field(min_length=1)
    kind: str  # PURE_GOLD | COIN | OUNCE | USED_PRODUCT

    # PURE_GOLD / USED_PRODUCT fields
    karat: str | None = None
    weight_grams: Decimal | None = Field(default=None, gt=0)

    # COIN / OUNCE fields
    coin_type_id: str | None = None
    ounce_type_id: str | None = None
    quantity: int | None = Field(default=None, gt=0)

    # Pricing knobs
    manual_price: Decimal | None = Field(default=None, ge=0)  # if set → MANUAL mode
    margin_mode: str | None = None  # override default; ignored if manual_price set
    margin_value: Decimal | None = None
    expected_rate: Decimal | None = None  # if set, server rejects on > drift threshold
    notes: str | None = None


class BuybackReceiptOut(BaseModel):
    """Returned after a successful POST. Drives receipt printing on the frontend."""
    id: str
    occurred_at: datetime
    seller_name: str
    seller_phone: str
    cashier_id: str
    kind: str
    karat: str | None
    weight_grams: Decimal | None
    quantity: int | None
    coin_type_id: str | None
    ounce_type_id: str | None
    result_lot_id: str | None
    product_id: str | None
    buy_price_usd: Decimal
    gold_rate_at_buy: Decimal
    buyback_margin_mode: str | None
    buyback_margin_value: Decimal | None
    price_mode: str
    notes: str | None

    model_config = {"from_attributes": True}


class BuybackListOut(BaseModel):
    items: list[BuybackReceiptOut]
    total: int
    page: int
    page_size: int
