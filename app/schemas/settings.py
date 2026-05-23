from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class SettingsOut(BaseModel):
    id: str
    store_name: str
    store_name_ar: str | None
    logo_url: str | None
    address: str
    phone: str
    vat_number: str | None
    default_margin_pct: Decimal
    default_making_charge: Decimal
    markup_k18: Decimal
    markup_k21: Decimal
    markup_k24: Decimal
    vat_percent: Decimal
    lbp_exchange_rate: Decimal
    receipt_footer: str | None
    gold_refresh_minutes: int
    default_buyback_margin_mode: str
    default_buyback_margin_value: Decimal
    buyback_rate_drift_pct_max: Decimal
    nisab_grams: Decimal
    updated_at: datetime

    model_config = {"from_attributes": True}


class SettingsUpdate(BaseModel):
    store_name: str | None = None
    store_name_ar: str | None = None
    logo_url: str | None = None
    address: str | None = None
    phone: str | None = None
    vat_number: str | None = None
    default_margin_pct: Decimal | None = None
    default_making_charge: Decimal | None = None
    markup_k18: Decimal | None = None
    markup_k21: Decimal | None = None
    markup_k24: Decimal | None = None
    vat_percent: Decimal | None = None
    lbp_exchange_rate: Decimal | None = None
    receipt_footer: str | None = None
    gold_refresh_minutes: int | None = None
    default_buyback_margin_mode: str | None = None
    default_buyback_margin_value: Decimal | None = None
    buyback_rate_drift_pct_max: Decimal | None = None
    nisab_grams: Decimal | None = Field(default=None, gt=0)


class StaffCreate(BaseModel):
    email: str
    name: str
    password: str


class StaffUpdate(BaseModel):
    name: str | None = None
    password: str | None = None
    is_active: bool | None = None


class StaffOut(BaseModel):
    id: str
    email: str
    name: str
    role: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}
