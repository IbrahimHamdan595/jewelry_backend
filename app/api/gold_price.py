from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.gold_api import fetch_gold_rate, get_current_gold_rate
from app.core.permissions import require_admin
from app.deps import get_db
from app.models import GoldRateHistory, GoldRateOverride, User
from app.schemas.gold_rate import GoldRateHistoryPoint, GoldRateOut, OverrideRequest

router = APIRouter(prefix="/gold-price", tags=["gold-price"])


@router.get("", response_model=GoldRateOut)
async def current_rate(db: AsyncSession = Depends(get_db)):
    info = await get_current_gold_rate(db)
    r = info["rate"]
    return GoldRateOut(
        rate_24k=r,
        rate_21k=round(r * 0.875, 2),
        rate_18k=round(r * 0.750, 2),
        source=info["source"],
        fetched_at=info["fetched_at"],
        is_stale=info["is_stale"],
    )


@router.get("/history", response_model=list[GoldRateHistoryPoint])
async def history(range: str = "24h", db: AsyncSession = Depends(get_db)):
    delta_map = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
    delta = delta_map.get(range, timedelta(hours=24))
    since = datetime.now(timezone.utc) - delta

    rows = (
        await db.execute(
            select(GoldRateHistory)
            .where(GoldRateHistory.fetched_at >= since)
            .order_by(GoldRateHistory.fetched_at.asc())
        )
    ).scalars().all()

    return [GoldRateHistoryPoint(rate_24k=float(r.rate_24k), fetched_at=r.fetched_at) for r in rows]


@router.post("/refresh", dependencies=[Depends(require_admin)])
async def force_refresh(db: AsyncSession = Depends(get_db)):
    rate = await fetch_gold_rate()
    db.add(GoldRateHistory(rate_24k=rate.value, source=rate.source))
    await db.commit()
    return {"rate": float(rate.value), "source": rate.source}


@router.post("/override", dependencies=[Depends(require_admin)])
async def set_override(body: OverrideRequest, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)):
    await db.execute(
        GoldRateOverride.__table__.update()
        .where(GoldRateOverride.is_active.is_(True))
        .values(is_active=False, cleared_at=datetime.now(timezone.utc))
    )
    db.add(GoldRateOverride(rate_24k=body.rate_24k, set_by=user.id, is_active=True))
    await db.commit()
    return {"message": "Override set", "rate_24k": float(body.rate_24k)}


@router.delete("/override", dependencies=[Depends(require_admin)])
async def clear_override(db: AsyncSession = Depends(get_db)):
    await db.execute(
        GoldRateOverride.__table__.update()
        .where(GoldRateOverride.is_active.is_(True))
        .values(is_active=False, cleared_at=datetime.now(timezone.utc))
    )
    await db.commit()
    return {"message": "Override cleared"}
