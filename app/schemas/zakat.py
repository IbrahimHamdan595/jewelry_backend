from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class KaratBucketOut(BaseModel):
    karat: str
    grams_by_source: dict[str, Decimal]
    total_weight_grams: Decimal
    au_grams: Decimal


class ZakatHoldingsOut(BaseModel):
    by_karat: list[KaratBucketOut]
    total_au_grams: Decimal


class ZakatSummaryOut(BaseModel):
    holdings: ZakatHoldingsOut
    gold_rate_24k: Decimal
    gold_rate_source: str
    gold_rate_is_stale: bool
    gold_rate_fetched_at: datetime
    nisab_grams: Decimal
    meets_nisab: bool
    total_au_value_usd: Decimal
    zakat_au_grams: Decimal
    zakat_value_usd: Decimal


# ── Snapshot ─────────────────────────────────────────────────────────────────

class ZakatSnapshotCreate(BaseModel):
    assessment_date: date
    notes: str | None = Field(default=None, max_length=1000)


class ZakatSnapshotOut(BaseModel):
    id: str
    taken_at: datetime
    assessment_date: date
    taken_by_user_id: str
    notes: str | None
    gold_rate_24k_usd_per_gram: Decimal
    gold_rate_source: str
    nisab_grams_used: Decimal
    total_au_grams: Decimal
    total_au_value_usd: Decimal
    zakat_au_grams: Decimal
    zakat_value_usd: Decimal
    meets_nisab: bool
    breakdown_by_karat: dict
    integrity_hash: str
    # Computed on read by recomputing the hash from current field values.
    # False indicates the row has been tampered with at the DB layer.
    integrity_ok: bool

    model_config = {"from_attributes": True}


class ZakatSnapshotListOut(BaseModel):
    items: list[ZakatSnapshotOut]
    total: int
