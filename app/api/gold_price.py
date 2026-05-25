from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.gold_api import fetch_gold_rate, get_current_gold_rate
from app.core.ledger import (
    EVENT_GOLD_RATE_OVERRIDE_CLEARED,
    EVENT_GOLD_RATE_OVERRIDE_SET,
    EVENT_GOLD_RATE_REFRESH_TRIGGERED,
    record,
)
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


@router.post("/refresh")
async def force_refresh(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Force an immediate gold-rate fetch.

    AUDIT: writes GOLD_RATE_REFRESH_TRIGGERED so we know who pressed the
    refresh button and what came back, even if the value matches the poller's
    next scheduled tick.
    """
    rate = await fetch_gold_rate()
    history_row = GoldRateHistory(rate_24k=rate.value, source=rate.source)
    db.add(history_row)
    await db.flush()
    await record(
        db,
        event_type=EVENT_GOLD_RATE_REFRESH_TRIGGERED,
        actor_user_id=user.id,
        ref_type="gold_rate_history",
        ref_id=history_row.id,
        payload={"rate_24k": str(rate.value), "source": rate.source},
    )
    await db.commit()
    return {"rate": float(rate.value), "source": rate.source}


async def _get_active_override_rate(db: AsyncSession) -> str | None:
    """Return the currently-active override rate as a str, or None if no
    override is active. Used by set/clear to capture the prior value for
    the audit payload."""
    prior = (
        await db.execute(
            select(GoldRateOverride).where(GoldRateOverride.is_active.is_(True)).limit(1)
        )
    ).scalar_one_or_none()
    return str(prior.rate_24k) if prior else None


@router.post("/override")
async def set_override(
    body: OverrideRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Set a manual gold-rate override.

    AUDIT: writes GOLD_RATE_OVERRIDE_SET with the new rate, the prior
    active override rate (if any), and the human-supplied reason. The
    reason is required at the schema layer (min_length=3) so empty
    justifications can't slip through.
    """
    prior_rate = await _get_active_override_rate(db)

    await db.execute(
        GoldRateOverride.__table__.update()
        .where(GoldRateOverride.is_active.is_(True))
        .values(is_active=False, cleared_at=datetime.now(timezone.utc))
    )
    new_override = GoldRateOverride(rate_24k=body.rate_24k, set_by=user.id, is_active=True)
    db.add(new_override)
    await db.flush()
    await record(
        db,
        event_type=EVENT_GOLD_RATE_OVERRIDE_SET,
        actor_user_id=user.id,
        ref_type="gold_rate_override",
        ref_id=new_override.id,
        payload={
            "rate_24k": str(body.rate_24k),
            "prior_rate_24k": prior_rate,  # may be None if no prior override
            "reason": body.reason,
        },
    )
    await db.commit()
    return {"message": "Override set", "rate_24k": float(body.rate_24k)}


@router.delete("/override")
async def clear_override(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Clear the active gold-rate override.

    AUDIT: writes GOLD_RATE_OVERRIDE_CLEARED with the prior rate. No-op
    when there's nothing to clear; in that case we still write a ledger
    row so an auditor sees the (deliberate) click even if it had no effect.
    """
    prior_rate = await _get_active_override_rate(db)

    await db.execute(
        GoldRateOverride.__table__.update()
        .where(GoldRateOverride.is_active.is_(True))
        .values(is_active=False, cleared_at=datetime.now(timezone.utc))
    )
    await record(
        db,
        event_type=EVENT_GOLD_RATE_OVERRIDE_CLEARED,
        actor_user_id=user.id,
        ref_type="gold_rate_override",
        # No specific override-row id when there was nothing active; use
        # a sentinel so the FK-less ref_id column stays consistent.
        ref_id="(no-active-override)" if prior_rate is None else "(cleared)",
        payload={"prior_rate_24k": prior_rate},
    )
    await db.commit()
    return {"message": "Override cleared"}
