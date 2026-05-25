from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class StockTakeCreate(BaseModel):
    notes: str | None = Field(default=None, max_length=1000)


class StockTakeLineCreate(BaseModel):
    ref_type: str   # "COIN_STOCK" or "OUNCE_STOCK" — validated server-side
    ref_id: str
    counted_qty: int = Field(ge=0)


class StockTakeLineUpdate(BaseModel):
    counted_qty: int = Field(ge=0)


class StockTakeLineReject(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class StockTakeLineOut(BaseModel):
    id: str
    stock_take_id: str
    ref_type: str
    ref_id: str
    counted_qty: int
    expected_qty_at_submit: int | None
    variance: int | None
    resolution: str
    rejection_reason: str | None
    adjustment_id: str | None
    resolved_at: datetime | None
    resolved_by_user_id: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class StockTakeOut(BaseModel):
    id: str
    started_at: datetime
    started_by_user_id: str
    submitted_at: datetime | None
    closed_at: datetime | None
    status: str
    notes: str | None
    lines: list[StockTakeLineOut]

    model_config = {"from_attributes": True}


class StockTakeListItem(BaseModel):
    id: str
    started_at: datetime
    started_by_user_id: str
    submitted_at: datetime | None
    closed_at: datetime | None
    status: str
    notes: str | None
    # Summary stats avoid sending all lines in the list view.
    line_count: int
    variance_line_count: int
    approved_count: int
    rejected_count: int

    model_config = {"from_attributes": True}


class StockTakeListOut(BaseModel):
    items: list[StockTakeListItem]
    total: int
    page: int
    page_size: int
