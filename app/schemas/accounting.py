from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class AccountCreate(BaseModel):
    code: str
    name: str
    type: str  # ASSET/LIABILITY/EQUITY/INCOME/EXPENSE
    denomination: str = "MONEY"
    normal_balance: str  # DEBIT/CREDIT
    parent_id: str | None = None
    currency: str | None = "USD"


class AccountUpdate(BaseModel):
    name: str | None = None
    parent_id: str | None = None
    is_active: bool | None = None


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    code: str
    name: str
    type: str
    denomination: str
    normal_balance: str
    parent_id: str | None
    currency: str | None
    system_key: str | None
    is_active: bool


class JournalLineIn(BaseModel):
    account_id: str
    money_debit: Decimal = Decimal("0")
    money_credit: Decimal = Decimal("0")
    currency: str = "USD"
    fx_rate: Decimal = Decimal("1")
    base_debit: Decimal = Decimal("0")
    base_credit: Decimal = Decimal("0")
    metal_debit_grams: Decimal = Decimal("0")
    metal_credit_grams: Decimal = Decimal("0")
    karat: str | None = None
    memo: str = ""


class JournalEntryCreate(BaseModel):
    entry_date: date
    memo: str = ""
    source_type: str = "MANUAL"
    source_id: str | None = None
    lines: list[JournalLineIn] = Field(min_length=1)


class JournalLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    line_no: int
    account_id: str
    money_debit: Decimal
    money_credit: Decimal
    currency: str
    fx_rate: Decimal
    base_debit: Decimal
    base_credit: Decimal
    metal_debit_grams: Decimal
    metal_credit_grams: Decimal
    karat: str | None
    memo: str


class JournalEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    entry_no: str
    entry_date: date
    period_id: str
    memo: str
    source_type: str
    source_id: str | None
    reverses_entry_id: str | None
    actor_user_id: str
    occurred_at: datetime
    prev_hash: str
    entry_hash: str
    lines: list[JournalLineOut] = []


class PeriodOpen(BaseModel):
    year: int
    period_no: int = Field(ge=1, le=12)


class PeriodOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    year: int
    period_no: int
    status: str
    closed_at: datetime | None


class OpeningCashLine(BaseModel):
    system_key: str
    amount: Decimal


class OpeningBalancesCreate(BaseModel):
    as_of: date
    cash_lines: list[OpeningCashLine] = []
