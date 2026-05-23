from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import require_admin
from app.core.zakat import ZakatSummary, compute_zakat_summary
from app.deps import get_db
from app.schemas.zakat import KaratBucketOut, ZakatHoldingsOut, ZakatSummaryOut

router = APIRouter(prefix="/zakat", tags=["zakat"], dependencies=[Depends(require_admin)])


def _to_out(summary: ZakatSummary) -> ZakatSummaryOut:
    return ZakatSummaryOut(
        holdings=ZakatHoldingsOut(
            by_karat=[
                KaratBucketOut(
                    karat=b.karat.value,
                    grams_by_source=b.grams_by_source,
                    total_weight_grams=b.total_weight_grams,
                    au_grams=b.au_grams,
                )
                for b in summary.holdings.by_karat
            ],
            total_au_grams=summary.holdings.total_au_grams,
        ),
        gold_rate_24k=summary.gold_rate_24k,
        gold_rate_source=summary.gold_rate_source,
        gold_rate_is_stale=summary.gold_rate_is_stale,
        gold_rate_fetched_at=summary.gold_rate_fetched_at,
        nisab_grams=summary.nisab_grams,
        meets_nisab=summary.meets_nisab,
        total_au_value_usd=summary.total_au_value_usd,
        zakat_au_grams=summary.zakat_au_grams,
        zakat_value_usd=summary.zakat_value_usd,
    )


@router.get("", response_model=ZakatSummaryOut)
async def live_zakat(db: AsyncSession = Depends(get_db)):
    """Live zakat summary recomputed from current inventory on every request."""
    try:
        summary = await compute_zakat_summary(db)
    except RuntimeError as e:
        # Raised by get_current_gold_rate when no rate is available at all.
        raise HTTPException(status_code=503, detail=str(e))
    return _to_out(summary)
