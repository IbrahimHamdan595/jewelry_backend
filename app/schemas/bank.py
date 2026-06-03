from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class BankAccountCreate(BaseModel):
    name: str
    account_type: str  # CASH/BANK/PETTY_CASH
    currency: str = "USD"
    bank_name: str | None = None
    account_number: str | None = None


class BankAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    gl_account_id: str
    account_type: str
    currency: str
    bank_name: str | None
    account_number: str | None
    last_reconciled_at: datetime | None
    is_active: bool


class TransferCreate(BaseModel):
    from_account_id: str
    to_account_id: str
    amount: Decimal = Field(gt=0)
    dest_amount: Decimal | None = None
    memo: str = ""
    entry_date: date


class ReconciliationStart(BaseModel):
    bank_account_id: str
    statement_date: date
    statement_balance: Decimal


class MatchRequest(BaseModel):
    statement_line_id: str
    gl_line_id: str
