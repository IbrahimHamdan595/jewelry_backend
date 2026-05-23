from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


# ── Supplier master ────────────────────────────────────────────────────────────


class SupplierCreate(BaseModel):
    name: str = Field(min_length=1)
    contact_name: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    default_currency: str = "USD"
    payment_terms: str | None = None
    notes: str | None = None


class SupplierUpdate(BaseModel):
    name: str | None = None
    contact_name: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    default_currency: str | None = None
    payment_terms: str | None = None
    notes: str | None = None
    is_active: bool | None = None


class SupplierOut(BaseModel):
    id: str
    name: str
    contact_name: str | None
    phone: str | None
    email: str | None
    address: str | None
    default_currency: str
    payment_terms: str | None
    notes: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SupplierListOut(BaseModel):
    items: list[SupplierOut]
    total: int
    page: int
    page_size: int


# ── Purchase (header + items) ──────────────────────────────────────────────────


class PurchaseItemIn(BaseModel):
    """Single line on an inbound supplier shipment.

    PRODUCT: provide product spec (name_en/ar, category, karat, weight_grams,
             margin_percent, making_charge, photos) plus unit_cost_usd. A new
             Product row is created.
    COIN:    provide coin_type_id + quantity. on_hand_qty incremented.
    OUNCE:   provide ounce_type_id + quantity. on_hand_qty incremented.
    PURE_GOLD: provide weight_grams + karat. A new gold_lot is created
               with cost_basis_usd = unit_cost_usd.
    """
    item_kind: str
    unit_cost_usd: Decimal = Field(ge=0)
    notes: str | None = None

    # Per-kind fields
    product: dict | None = None  # PRODUCT
    coin_type_id: str | None = None
    ounce_type_id: str | None = None
    quantity: int | None = Field(default=None, gt=0)
    weight_grams: Decimal | None = Field(default=None, gt=0)
    karat: str | None = None


class GoldPaymentIn(BaseModel):
    lot_id: str
    grams: Decimal = Field(gt=0)
    karat: str


class PurchaseCreate(BaseModel):
    occurred_at: datetime | None = None
    payment_mode: str  # CASH | GOLD | MIXED
    trade_markup_per_gram: Decimal | None = None
    total_cash_due: Decimal = Field(ge=0, default=Decimal("0"))
    total_grams_due_by_karat: dict[str, Decimal] = Field(default_factory=dict)
    cash_paid_at_creation: Decimal = Field(ge=0, default=Decimal("0"))
    gold_payments_at_creation: list[GoldPaymentIn] = Field(default_factory=list)
    items: list[PurchaseItemIn] = Field(default_factory=list)
    notes: str | None = None


class PurchaseItemOut(BaseModel):
    id: str
    item_kind: str
    product_id: str | None
    coin_type_id: str | None
    ounce_type_id: str | None
    lot_id: str | None
    quantity: int | None
    weight_grams: Decimal | None
    karat: str | None
    unit_cost_usd: Decimal
    notes: str | None

    model_config = {"from_attributes": True}


class PurchaseOut(BaseModel):
    id: str
    supplier_id: str
    occurred_at: datetime
    payment_mode: str
    trade_markup_per_gram: Decimal | None
    total_cash_due: Decimal
    total_grams_due_by_karat: dict
    cash_paid_at_creation: Decimal
    grams_paid_at_creation_by_karat: dict
    notes: str | None
    created_by_user_id: str
    created_at: datetime
    items: list[PurchaseItemOut]

    model_config = {"from_attributes": True}


# ── Repayment ──────────────────────────────────────────────────────────────────


class PaymentCreate(BaseModel):
    unit: str  # CASH | GOLD
    karat: str | None = None  # required when unit=GOLD
    amount: Decimal = Field(gt=0)
    gold_payments: list[GoldPaymentIn] = Field(default_factory=list)  # for GOLD only
    notes: str | None = None


class PaymentOut(BaseModel):
    id: str
    supplier_id: str
    paid_at: datetime
    unit: str
    karat: str | None
    amount: Decimal
    source_lot_ids: list | None
    paid_by_user_id: str
    notes: str | None

    model_config = {"from_attributes": True}


# ── Balances + AP ──────────────────────────────────────────────────────────────


class BalanceOut(BaseModel):
    unit: str
    karat: str | None  # None for CASH
    balance: Decimal


class SupplierDetailOut(BaseModel):
    supplier: SupplierOut
    balances: list[BalanceOut]
    purchases: list[PurchaseOut]
    payments: list[PaymentOut]


class APSupplierRow(BaseModel):
    supplier_id: str
    supplier_name: str
    balances: list[BalanceOut]


class AccountsPayableOut(BaseModel):
    total_cash_owed: Decimal
    total_grams_owed_by_karat: dict[str, Decimal]
    suppliers: list[APSupplierRow]
