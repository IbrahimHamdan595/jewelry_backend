from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


class BillLineIn(BaseModel):
    description: str = ""
    expense_account_id: str | None = None
    expense_system_key: str | None = None
    amount: Decimal = Field(gt=0)


class VendorBillCreate(BaseModel):
    vendor_name: str
    supplier_id: str | None = None
    bill_date: date
    due_date: date | None = None
    memo: str = ""
    payment_system_key: str | None = None  # CASH/BANK ⇒ paid now; None ⇒ on credit
    tax_code_id: str | None = None  # Module 6 — input VAT
    currency: str = "USD"
    fx_rate: Decimal | None = None  # LBP per USD; default 1 (USD) or settings rate
    lines: list[BillLineIn] = Field(min_length=1)


class VendorPaymentCreate(BaseModel):
    vendor_name: str
    payment_date: date
    amount: Decimal = Field(gt=0)
    payment_system_key: str = "CASH"
    memo: str = ""
    allocations: list[dict] | None = None
    currency: str = "USD"            # currency the payment is recorded in (the bill currency)
    fx_rate: Decimal | None = None   # cash-leg rate (LBP per USD); default 1 / settings rate
