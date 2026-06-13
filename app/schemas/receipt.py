"""Shared, normalized receipt shape (Phase 0).

A single `ReceiptOut` schema that all three printable transaction types render
through: a customer SALE, a SUPPLIER_PURCHASE, and a walk-in BUYBACK. The
frontend `<Receipt />` component consumes exactly this shape, so adding a new
receipt source means writing one builder (see `app/core/receipt.py`) — never a
new template.

Money/weight values are `Decimal` and serialize as strings to preserve the
quantize() precision the rest of the app relies on. Optional fields are `None`
when not applicable to a given receipt type (e.g. a buyback has no VAT line).
"""
from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class ReceiptType(str, enum.Enum):
    SALE = "SALE"
    SUPPLIER_PURCHASE = "SUPPLIER_PURCHASE"
    BUYBACK = "BUYBACK"


class ReceiptStore(BaseModel):
    """Store header, sourced from the `Settings` singleton."""
    name: str
    name_ar: str | None = None
    logo_url: str | None = None
    address: str = ""
    phone: str = ""
    vat_number: str | None = None
    footer: str | None = None


class ReceiptParty(BaseModel):
    """The counterparty on the receipt. `role` distinguishes the three types:
    customer (SALE), supplier (SUPPLIER_PURCHASE), seller (BUYBACK)."""
    role: str  # "customer" | "supplier" | "seller"
    name: str | None = None
    phone: str | None = None


class ReceiptLine(BaseModel):
    description: str
    description_ar: str | None = None
    code: str | None = None
    karat: str | None = None
    weight_grams: Decimal | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    stone_value: Decimal | None = None
    line_total: Decimal


class ReceiptTotals(BaseModel):
    subtotal: Decimal
    # Phase 2 discount fields — None on receipts created before discounts existed
    # and on non-sale receipts.
    discount_percent: Decimal | None = None
    discount_amount: Decimal | None = None
    vat_percent: Decimal | None = None
    vat_amount: Decimal | None = None
    total_usd: Decimal
    total_lbp: Decimal | None = None
    lbp_exchange_rate: Decimal | None = None


class ReceiptOut(BaseModel):
    type: ReceiptType
    reference: str           # order_number / purchase id / buyback id
    issued_at: datetime
    store: ReceiptStore
    cashier_name: str | None = None
    party: ReceiptParty
    lines: list[ReceiptLine]
    totals: ReceiptTotals
    payment_method: str | None = None
    notes: str | None = None
