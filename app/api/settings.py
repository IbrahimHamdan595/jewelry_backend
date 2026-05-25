from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ledger import EVENT_SETTINGS_CHANGED, field_diff, record
from app.core.permissions import require_admin
from app.deps import get_current_user, get_db
from app.models import Settings, User
from app.schemas.settings import SettingsOut, SettingsUpdate

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_model=SettingsOut)
async def get_settings(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    s = (await db.execute(select(Settings).where(Settings.id == "singleton"))).scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Settings not found")
    return SettingsOut.model_validate(s)


@router.patch("", response_model=SettingsOut)
async def update_settings(
    body: SettingsUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Update the store-config singleton.

    AUDIT: every change to a financial knob (VAT %, LBP rate, karat
    markups, nisab, buyback margin, etc.) writes a SETTINGS_CHANGED ledger
    row carrying a per-field {from, to} diff. No-op PATCHes that don't
    actually change anything are NOT recorded — the absence of a diff is
    the absence of an event.
    """
    s = (await db.execute(select(Settings).where(Settings.id == "singleton"))).scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Settings not found")

    incoming = body.model_dump(exclude_unset=True)
    # Snapshot only the fields the caller is trying to change so the diff
    # stays focused. SettingsOut.model_dump() would include 20+ fields most
    # of which the caller never touched.
    before = {field: getattr(s, field) for field in incoming.keys()}

    for field, value in incoming.items():
        setattr(s, field, value)

    after = {field: getattr(s, field) for field in incoming.keys()}
    diff = field_diff(before, after)

    if diff:
        await record(
            db,
            event_type=EVENT_SETTINGS_CHANGED,
            actor_user_id=user.id,
            ref_type="settings",
            ref_id=s.id,  # always "singleton"
            payload={"diff": diff},
        )

    await db.commit()
    await db.refresh(s)
    return SettingsOut.model_validate(s)
