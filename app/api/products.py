from decimal import Decimal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.r2 import upload_image
from app.core.gold_api import get_current_gold_rate
from app.core.ledger import (
    record,
    EVENT_PRODUCT_CREATED,
    EVENT_PRODUCT_UPDATED,
    EVENT_PRODUCT_DELETED,
)
from app.core.permissions import require_admin
from app.core.pricing import KARAT_PURITY, calculate_price, generate_item_code
from app.deps import get_current_user, get_db
from app.models import Karat, Product, ProductStatus, Settings, User
from app.schemas.product import (
    ProductCreate, ProductListOut, ProductLookupOut, ProductOut, ProductUpdate,
)

router = APIRouter(prefix="/products", tags=["products"])


@router.get("", response_model=ProductListOut)
async def list_products(
    search: str = "",
    category: str = "",
    category_id: str = "",
    karat: str = "",
    status: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = select(Product)
    if search:
        q = q.where(
            Product.name_en.ilike(f"%{search}%") | Product.code.ilike(f"%{search}%")
        )
    if category:
        q = q.where(Product.category == category)
    if category_id:
        q = q.where(Product.category_id == category_id)
    if karat:
        q = q.where(Product.karat == karat)
    if status == "active":
        q = q.where(Product.is_active.is_(True))
    elif status == "inactive":
        q = q.where(Product.is_active.is_(False))

    total_result = await db.execute(select(func.count()).select_from(q.subquery()))
    total = total_result.scalar_one()

    q = q.order_by(Product.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    products = (await db.execute(q)).scalars().all()

    return ProductListOut(
        items=[ProductOut.model_validate(p) for p in products],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("", response_model=ProductOut, status_code=201)
async def create_product(
    body: ProductCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    code = await generate_item_code(db, Karat(body.karat))
    product = Product(
        code=code,
        name_en=body.name_en,
        name_ar=body.name_ar,
        category=body.category,
        karat=Karat(body.karat),
        weight_grams=body.weight_grams,
        margin_percent=body.margin_percent,
        making_charge=body.making_charge,
        photos=body.photos,
        on_hand_qty=body.on_hand_qty,
        min_stock_qty=body.min_stock_qty,
        status=ProductStatus.AVAILABLE if body.on_hand_qty > 0 else ProductStatus.SOLD,
        stone_value_usd=body.stone_value_usd,
        stone_cost_usd=body.stone_cost_usd,
        stone_carats=body.stone_carats,
        stone_count=body.stone_count,
        stone_cert=body.stone_cert,
        stone_note=body.stone_note,
    )
    db.add(product)
    await db.flush()
    await record(
        db,
        event_type=EVENT_PRODUCT_CREATED,
        actor_user_id=user.id,
        ref_type="product",
        ref_id=product.id,
        payload={"code": product.code, "name_en": product.name_en, "karat": product.karat.value},
    )
    await db.commit()
    await db.refresh(product)
    return ProductOut.model_validate(product)


@router.post("/upload-image", dependencies=[Depends(require_admin)])
async def upload_product_image(file: UploadFile = File(...)):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image too large (max 10 MB)")
    url = await upload_image(content, file.filename or "upload", file.content_type)
    return {"url": url}


@router.get("/lookup/{code}", response_model=ProductLookupOut)
async def lookup_product(code: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    product = (
        await db.execute(
            select(Product).where(
                Product.code == code,
                Product.is_active.is_(True),
                Product.status == ProductStatus.AVAILABLE,
            )
        )
    ).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail=f"Product '{code}' not available")

    rate_info = await get_current_gold_rate(db)
    rate_24k = Decimal(str(rate_info["rate"]))

    cfg = (await db.execute(select(Settings).where(Settings.id == "singleton"))).scalar_one_or_none()
    markup_map = {
        "K18": cfg.markup_k18 if cfg else Decimal("0"),
        "K21": cfg.markup_k21 if cfg else Decimal("0"),
        "K24": cfg.markup_k24 if cfg else Decimal("0"),
    }
    karat_markup = markup_map.get(product.karat.value, Decimal("0"))

    priced = calculate_price(
        rate_24k=rate_24k,
        karat=product.karat,
        weight_grams=product.weight_grams,
        margin_percent=product.margin_percent,
        making_charge=product.making_charge,
        karat_markup=karat_markup,
    )

    photos = product.photos or []
    hero = next((p for p in photos if p.get("isHero")), None) or (photos[0] if photos else None)
    photo_url = hero.get("url") if hero else None

    return ProductLookupOut(
        id=product.id,
        code=product.code,
        name_en=product.name_en,
        name_ar=product.name_ar,
        karat=product.karat.value,
        weight_grams=product.weight_grams,
        margin_percent=product.margin_percent,
        making_charge=product.making_charge,
        gold_rate_24k=rate_info["rate"],
        purity_rate=priced["purity_rate"],
        final_price=priced["final_price"],
        on_hand_qty=product.on_hand_qty,
        photo_url=photo_url,
    )


@router.get("/{product_id}", response_model=ProductOut)
async def get_product(product_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    product = (await db.execute(select(Product).where(Product.id == product_id))).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return ProductOut.model_validate(product)


@router.patch("/{product_id}", response_model=ProductOut)
async def update_product(
    product_id: str,
    body: ProductUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    product = (await db.execute(select(Product).where(Product.id == product_id))).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(product, field, value)
    await db.flush()
    await record(
        db,
        event_type=EVENT_PRODUCT_UPDATED,
        actor_user_id=user.id,
        ref_type="product",
        ref_id=product.id,
        payload={"changes": {k: str(v) for k, v in changes.items()}},
    )
    await db.commit()
    await db.refresh(product)
    return ProductOut.model_validate(product)


@router.patch("/{product_id}/status", response_model=ProductOut)
async def toggle_status(
    product_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    product = (await db.execute(select(Product).where(Product.id == product_id))).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    product.is_active = not product.is_active
    await db.commit()
    await db.refresh(product)
    return ProductOut.model_validate(product)


@router.delete("/{product_id}", status_code=204)
async def delete_product(
    product_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    product = (await db.execute(select(Product).where(Product.id == product_id))).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    product.is_active = False
    await db.flush()
    await record(
        db,
        event_type=EVENT_PRODUCT_DELETED,
        actor_user_id=user.id,
        ref_type="product",
        ref_id=product.id,
        payload={"soft_delete": True},
    )
    await db.commit()
