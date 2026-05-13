from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class OrderItemIn(BaseModel):
    product_id: str
    quantity: int = 1


class CheckoutRequest(BaseModel):
    items: list[OrderItemIn]
    payment_method: str
    customer_name: str | None = None


class OrderItemOut(BaseModel):
    id: str
    product_id: str
    product_code: str
    product_name: str
    karat: str
    weight_grams: Decimal
    gold_rate_at_sale: Decimal
    margin_percent: Decimal
    making_charge: Decimal
    final_price: Decimal

    model_config = {"from_attributes": True}


class CashierOut(BaseModel):
    id: str
    name: str
    email: str

    model_config = {"from_attributes": True}


class OrderOut(BaseModel):
    id: str
    order_number: str
    status: str
    payment_method: str
    customer_name: str | None
    cashier_id: str
    cashier: CashierOut
    subtotal: Decimal
    vat_percent: Decimal
    vat_amount: Decimal
    total_usd: Decimal
    total_lbp: Decimal
    lbp_exchange_rate: Decimal
    voided_at: datetime | None
    voided_by: str | None
    void_reason: str | None
    created_at: datetime
    items: list[OrderItemOut]

    model_config = {"from_attributes": True}


class OrderSummaryOut(BaseModel):
    id: str
    order_number: str
    status: str
    payment_method: str
    customer_name: str | None
    cashier: CashierOut
    total_usd: Decimal
    total_lbp: Decimal
    item_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


class VoidRequest(BaseModel):
    reason: str


class OrderListOut(BaseModel):
    items: list[OrderSummaryOut]
    total: int
    total_revenue: Decimal
    avg_order_value: Decimal
