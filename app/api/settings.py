from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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


@router.patch("", response_model=SettingsOut, dependencies=[Depends(require_admin)])
async def update_settings(body: SettingsUpdate, db: AsyncSession = Depends(get_db)):
    s = (await db.execute(select(Settings).where(Settings.id == "singleton"))).scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Settings not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(s, field, value)
    await db.commit()
    await db.refresh(s)
    return SettingsOut.model_validate(s)
