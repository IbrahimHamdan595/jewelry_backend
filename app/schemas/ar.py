from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class CustomerCreate(BaseModel):
    name: str
    phone: str | None = None
    email: str | None = None
    currency: str = "USD"
    credit_limit: Decimal | None = None
    notes: str | None = None


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    phone: str | None
    email: str | None
    currency: str
    credit_limit: Decimal | None
    is_active: bool


class InvoiceLineIn(BaseModel):
    description: str = ""
    quantity: int = 1
    unit_price: Decimal


class StandaloneInvoiceCreate(BaseModel):
    customer_id: str
    invoice_date: date
    due_date: date | None = None
    vat_percent: Decimal = Decimal("0")
    memo: str = ""
    fx_rate: Decimal | None = None  # LBP per USD; default 1 (USD) or settings rate for an LBP customer
    lines: list[InvoiceLineIn] = Field(min_length=1)


class ReceiptCreate(BaseModel):
    customer_id: str
    receipt_date: date
    amount: Decimal = Field(gt=0)
    payment_system_key: str = "CASH"
    memo: str = ""
    allocations: list[dict] | None = None
    currency: str = "USD"            # currency the receipt is recorded in (the invoice currency)
    fx_rate: Decimal | None = None   # cash-leg rate (LBP per USD); default 1 / settings rate
