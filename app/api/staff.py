from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ledger import EVENT_STAFF_CREATED, EVENT_STAFF_UPDATED, field_diff, record
from app.core.permissions import require_admin
from app.core.security import hash_password
from app.deps import get_db
from app.models import Role, User
from app.schemas.settings import StaffCreate, StaffOut, StaffUpdate

router = APIRouter(prefix="/staff", tags=["staff"])


@router.get("", response_model=list[StaffOut], dependencies=[Depends(require_admin)])
async def list_staff(db: AsyncSession = Depends(get_db)):
    users = (await db.execute(select(User).where(User.role == Role.CASHIER).order_by(User.created_at.desc()))).scalars().all()
    return [StaffOut.model_validate(u) for u in users]


@router.post("", response_model=StaffOut, status_code=201)
async def create_staff(
    body: StaffCreate,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_admin),
):
    """Create a cashier account.

    AUDIT: STAFF_CREATED carries email, name, role, and is_active. Password
    hash is deliberately NOT in the payload (the ledger is admin-visible and
    we don't want even a bcrypt hash exposed there).
    """
    existing = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Email already in use")

    user = User(
        email=body.email,
        name=body.name,
        password_hash=hash_password(body.password),
        role=Role.CASHIER,
    )
    db.add(user)
    await db.flush()
    await record(
        db,
        event_type=EVENT_STAFF_CREATED,
        actor_user_id=actor.id,
        ref_type="user",
        ref_id=user.id,
        payload={
            "email": user.email,
            "name": user.name,
            "role": user.role.value,
            "is_active": user.is_active,
        },
    )
    await db.commit()
    await db.refresh(user)
    return StaffOut.model_validate(user)


@router.patch("/{user_id}", response_model=StaffOut)
async def update_staff(
    user_id: str,
    body: StaffUpdate,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_admin),
):
    """Update a cashier account.

    AUDIT: STAFF_UPDATED carries a per-field {from, to} diff. Password
    changes are recorded as `{"password_hash": {"from": "***", "to": "***"}}`
    — we log THAT the password changed without leaking the hash itself.
    """
    user = (await db.execute(select(User).where(User.id == user_id, User.role == Role.CASHIER))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Staff not found")

    # Snapshot the auditable fields before mutation. password_hash is
    # tracked but masked in the payload.
    before = {
        "name": user.name,
        "is_active": user.is_active,
        "password_hash": user.password_hash,
    }

    if body.name is not None:
        user.name = body.name
    if body.password is not None:
        user.password_hash = hash_password(body.password)
    if body.is_active is not None:
        user.is_active = body.is_active

    after = {
        "name": user.name,
        "is_active": user.is_active,
        "password_hash": user.password_hash,
    }
    diff = field_diff(before, after)

    # Mask the password hash — record THAT it changed, not WHAT it changed to.
    if "password_hash" in diff:
        diff["password_hash"] = {"from": "***", "to": "***"}

    if diff:
        await record(
            db,
            event_type=EVENT_STAFF_UPDATED,
            actor_user_id=actor.id,
            ref_type="user",
            ref_id=user.id,
            payload={"diff": diff},
        )

    await db.commit()
    await db.refresh(user)
    return StaffOut.model_validate(user)


@router.delete("/{user_id}", status_code=204)
async def delete_staff(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_admin),
):
    """Soft-delete a cashier by flipping is_active=False.

    AUDIT: records STAFF_UPDATED (not a separate "deleted" event) because
    this IS just an update to is_active — the row stays. Idempotent on
    already-disabled users (no ledger row if nothing changed).
    """
    user = (await db.execute(select(User).where(User.id == user_id, User.role == Role.CASHIER))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Staff not found")

    if user.is_active:
        user.is_active = False
        await db.flush()
        await record(
            db,
            event_type=EVENT_STAFF_UPDATED,
            actor_user_id=actor.id,
            ref_type="user",
            ref_id=user.id,
            payload={"diff": {"is_active": {"from": True, "to": False}}},
        )
    await db.commit()
