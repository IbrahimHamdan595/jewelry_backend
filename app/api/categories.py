from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ledger import (
    EVENT_CATEGORY_CREATED,
    EVENT_CATEGORY_DELETED,
    EVENT_CATEGORY_UPDATED,
    record,
)
from app.core.permissions import require_admin
from app.deps import get_current_user, get_db
from app.models import Category, User
from app.schemas.category import CategoryCreate, CategoryOut, CategoryUpdate

router = APIRouter(prefix="/categories", tags=["categories"])


@router.get("", response_model=list[CategoryOut])
async def list_categories(
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = select(Category).order_by(Category.name_en)
    if not include_inactive:
        q = q.where(Category.is_active.is_(True))
    return (await db.execute(q)).scalars().all()


@router.post("", response_model=CategoryOut, status_code=201)
async def create_category(
    body: CategoryCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    existing = (await db.execute(select(Category).where(Category.slug == body.slug))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Category slug already exists")
    cat = Category(name_en=body.name_en, name_ar=body.name_ar, slug=body.slug)
    db.add(cat)
    await db.flush()
    await record(
        db,
        event_type=EVENT_CATEGORY_CREATED,
        actor_user_id=user.id,
        ref_type="category",
        ref_id=cat.id,
        payload={"name_en": cat.name_en, "slug": cat.slug},
    )
    await db.commit()
    await db.refresh(cat)
    return cat


@router.patch("/{category_id}", response_model=CategoryOut)
async def update_category(
    category_id: str,
    body: CategoryUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    cat = (await db.execute(select(Category).where(Category.id == category_id))).scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(cat, field, value)
    await db.flush()
    await record(
        db,
        event_type=EVENT_CATEGORY_UPDATED,
        actor_user_id=user.id,
        ref_type="category",
        ref_id=cat.id,
        payload={"changes": {k: str(v) for k, v in changes.items()}},
    )
    await db.commit()
    await db.refresh(cat)
    return cat


@router.delete("/{category_id}", status_code=204)
async def delete_category(
    category_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    cat = (await db.execute(select(Category).where(Category.id == category_id))).scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    cat.is_active = False
    await db.flush()
    await record(
        db,
        event_type=EVENT_CATEGORY_DELETED,
        actor_user_id=user.id,
        ref_type="category",
        ref_id=cat.id,
        payload={"soft_delete": True},
    )
    await db.commit()
