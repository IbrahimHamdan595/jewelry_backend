from datetime import datetime

from pydantic import BaseModel


class LedgerEntryOut(BaseModel):
    id: str
    event_type: str
    actor_user_id: str
    occurred_at: datetime
    ref_type: str
    ref_id: str
    payload: dict
    created_at: datetime

    model_config = {"from_attributes": True}


class LedgerListOut(BaseModel):
    items: list[LedgerEntryOut]
    total: int
    page: int
    page_size: int
