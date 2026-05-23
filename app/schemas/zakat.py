from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


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
