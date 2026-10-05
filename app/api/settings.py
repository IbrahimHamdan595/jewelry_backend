from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.coa_seed import describe_unusable_accounts, unusable_system_accounts
from app.core.ledger import EVENT_SETTINGS_CHANGED, field_diff, record
from app.core.permissions import require_admin
from app.deps import get_current_user, get_db
from app.models import GLPeriod, PeriodStatus, Settings, User
from app.schemas.settings import SettingsOut, SettingsUpdate

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_model=SettingsOut)
async def get_settings(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    s = (await db.execute(select(Settings).where(Settings.id == "singleton"))).scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Settings not found")
    return SettingsOut.model_validate(s)


async def _assert_ready_for_auto_post(db: AsyncSession) -> None:
    """Once the switch is ON every sale posts inside its own transaction, so a
    posting that cannot succeed is a sale that cannot be rung up. Refuse to
    switch on while the very next sale would fail, and say why (409)."""
    problems = await unusable_system_accounts(db)
    if problems:
        raise HTTPException(
            status_code=409,
            detail=(
                f"The chart of accounts is not ready: {describe_unusable_accounts(problems)}. "
                "Seed or reactivate them under Accounting › Chart of accounts before "
                "turning auto-posting on."
            ),
        )
    # A sale is booked on the UTC date of Order.created_at. A missing period is
    # fine (the first posting opens it); a CLOSED one refuses the posting.
    today = datetime.now(timezone.utc).date()
    period = (
        await db.execute(
            select(GLPeriod).where(GLPeriod.year == today.year, GLPeriod.period_no == today.month)
        )
    ).scalar_one_or_none()
    if period is not None and period.status != PeriodStatus.OPEN:
        raise HTTPException(
            status_code=409,
            detail=(
                f"The accounting period {today.year}-{today.month:02d} is CLOSED, so the next "
                "sale could not be posted. Reopen it under Accounting › Periods before "
                "turning auto-posting on."
            ),
        )


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
    # The GL auto-post master switch (NEX-52) only moves on an explicit
    # true/false. exclude_unset already drops it when another settings tab
    # saves without it; a null is dropped too, so a stale form can neither
    # reset the switch nor write NULL into its NOT NULL column.
    if incoming.get("accounting_auto_post_enabled", False) is None:
        del incoming["accounting_auto_post_enabled"]
    # Switching it ON is refused while the next sale could not post — before
    # anything in this PATCH is applied. Switching it OFF is never blocked.
    if incoming.get("accounting_auto_post_enabled") and not s.accounting_auto_post_enabled:
        await _assert_ready_for_auto_post(db)
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
