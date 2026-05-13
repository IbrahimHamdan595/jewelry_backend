from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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


@router.post("", response_model=StaffOut, status_code=201, dependencies=[Depends(require_admin)])
async def create_staff(body: StaffCreate, db: AsyncSession = Depends(get_db)):
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
    await db.commit()
    await db.refresh(user)
    return StaffOut.model_validate(user)


@router.patch("/{user_id}", response_model=StaffOut, dependencies=[Depends(require_admin)])
async def update_staff(user_id: str, body: StaffUpdate, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.id == user_id, User.role == Role.CASHIER))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Staff not found")

    if body.name is not None:
        user.name = body.name
    if body.password is not None:
        user.password_hash = hash_password(body.password)
    if body.is_active is not None:
        user.is_active = body.is_active

    await db.commit()
    await db.refresh(user)
    return StaffOut.model_validate(user)


@router.delete("/{user_id}", status_code=204, dependencies=[Depends(require_admin)])
async def delete_staff(user_id: str, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.id == user_id, User.role == Role.CASHIER))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Staff not found")
    user.is_active = False
    await db.commit()
