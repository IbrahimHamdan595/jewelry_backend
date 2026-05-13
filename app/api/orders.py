import csv
import io
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.gold_api import get_current_gold_rate
from app.core.permissions import require_admin
from app.core.pricing import calculate_price, generate_order_number
from app.deps import get_current_user, get_db
from app.models import Order, OrderItem, OrderStatus, PaymentMethod, Product, Settings, User
from app.schemas.order import (
    CheckoutRequest, OrderListOut, OrderOut, OrderSummaryOut, VoidRequest,
)

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("", response_model=OrderListOut)
async def list_orders(
    from_date: str = "",
    to_date: str = "",
    cashier: str = "",
    payment: str = "",
    status: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = select(Order).options(selectinload(Order.cashier))

    if from_date:
        q = q.where(Order.created_at >= datetime.fromisoformat(from_date))
    if to_date:
        q = q.where(Order.created_at <= datetime.fromisoformat(to_date))
    if cashier:
        q = q.where(Order.cashier_id == cashier)
    if payment:
        q = q.where(Order.payment_method == payment)
    if status:
        q = q.where(Order.status == status)

    total_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(total_q)).scalar_one()

    revenue_q = select(func.coalesce(func.sum(Order.total_usd), 0)).select_from(
        q.where(Order.status == OrderStatus.COMPLETED).subquery()
    )
    total_revenue = (await db.execute(revenue_q)).scalar_one()

    q = q.order_by(Order.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    orders = (await db.execute(q)).scalars().all()

    summaries = []
    for o in orders:
        summaries.append(OrderSummaryOut(
            id=o.id,
            order_number=o.order_number,
            status=o.status.value,
            payment_method=o.payment_method.value,
            customer_name=o.customer_name,
            cashier=o.cashier,
            total_usd=o.total_usd,
            total_lbp=o.total_lbp,
            item_count=len(o.items) if o.items else 0,
            created_at=o.created_at,
        ))

    avg = Decimal(str(total_revenue)) / total if total else Decimal(0)
    return OrderListOut(items=summaries, total=total, total_revenue=Decimal(str(total_revenue)), avg_order_value=avg)


@router.post("", response_model=OrderOut, status_code=201)
async def create_order(
    payload: CheckoutRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    settings = (await db.execute(select(Settings).where(Settings.id == "singleton"))).scalar_one_or_none()
    if not settings:
        raise HTTPException(status_code=500, detail="Settings not configured")

    rate_info = await get_current_gold_rate(db)
    rate_24k = Decimal(str(rate_info["rate"]))

    order_number = await generate_order_number(db, datetime.now(timezone.utc))

    order_items = []
    subtotal = Decimal(0)

    for line in payload.items:
        product = (await db.execute(select(Product).where(Product.id == line.product_id))).scalar_one_or_none()
        if not product or not product.is_active:
            raise HTTPException(status_code=400, detail=f"Invalid product {line.product_id}")

        for _ in range(line.quantity):
            priced = calculate_price(
                rate_24k=rate_24k,
                karat=product.karat,
                weight_grams=product.weight_grams,
                margin_percent=product.margin_percent,
                making_charge=product.making_charge,
            )
            order_items.append(OrderItem(
                product_id=product.id,
                product_code=product.code,
                product_name=product.name_en,
                karat=product.karat,
                weight_grams=product.weight_grams,
                gold_rate_at_sale=rate_24k,
                margin_percent=product.margin_percent,
                making_charge=product.making_charge,
                final_price=priced["final_price"],
            ))
            subtotal += priced["final_price"]

    vat_amount = (subtotal * settings.vat_percent / Decimal(100)).quantize(Decimal("0.01"))
    total_usd = subtotal + vat_amount
    total_lbp = (total_usd * settings.lbp_exchange_rate).quantize(Decimal("0.01"))

    order = Order(
        order_number=order_number,
        cashier_id=user.id,
        payment_method=PaymentMethod(payload.payment_method),
        customer_name=payload.customer_name,
        subtotal=subtotal,
        vat_percent=settings.vat_percent,
        vat_amount=vat_amount,
        total_usd=total_usd,
        total_lbp=total_lbp,
        lbp_exchange_rate=settings.lbp_exchange_rate,
        items=order_items,
    )

    db.add(order)
    await db.commit()
    await db.refresh(order)

    order_with_relations = (
        await db.execute(
            select(Order)
            .options(selectinload(Order.cashier), selectinload(Order.items))
            .where(Order.id == order.id)
        )
    ).scalar_one()

    return OrderOut.model_validate(order_with_relations)


@router.get("/export")
async def export_orders(
    from_date: str = "",
    to_date: str = "",
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    q = select(Order).options(selectinload(Order.cashier), selectinload(Order.items))
    if from_date:
        q = q.where(Order.created_at >= datetime.fromisoformat(from_date))
    if to_date:
        q = q.where(Order.created_at <= datetime.fromisoformat(to_date))
    q = q.order_by(Order.created_at.desc())
    orders = (await db.execute(q)).scalars().all()

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Order #", "Date", "Cashier", "Items", "Subtotal USD", "VAT", "Total USD", "Total LBP", "Payment", "Status"])
        for o in orders:
            writer.writerow([
                o.order_number,
                o.created_at.isoformat(),
                o.cashier.name,
                len(o.items),
                float(o.subtotal),
                float(o.vat_amount),
                float(o.total_usd),
                float(o.total_lbp),
                o.payment_method.value,
                o.status.value,
            ])
            buf.seek(0)
            yield buf.read()
            buf.truncate(0)
            buf.seek(0)

    return StreamingResponse(generate(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=orders.csv"})


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(order_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    order = (
        await db.execute(
            select(Order)
            .options(selectinload(Order.cashier), selectinload(Order.items))
            .where(Order.id == order_id)
        )
    ).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return OrderOut.model_validate(order)


@router.post("/{order_id}/void", response_model=OrderOut)
async def void_order(
    order_id: str,
    body: VoidRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    order = (
        await db.execute(
            select(Order)
            .options(selectinload(Order.cashier), selectinload(Order.items))
            .where(Order.id == order_id)
        )
    ).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status == OrderStatus.VOIDED:
        raise HTTPException(status_code=400, detail="Order already voided")

    order.status = OrderStatus.VOIDED
    order.voided_at = datetime.now(timezone.utc)
    order.voided_by = user.id
    order.void_reason = body.reason
    await db.commit()
    await db.refresh(order)
    return OrderOut.model_validate(order)


@router.post("/{order_id}/refund", response_model=OrderOut)
async def refund_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    order = (
        await db.execute(
            select(Order)
            .options(selectinload(Order.cashier), selectinload(Order.items))
            .where(Order.id == order_id)
        )
    ).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status != OrderStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Only completed orders can be refunded")

    order.status = OrderStatus.REFUNDED
    await db.commit()
    await db.refresh(order)
    return OrderOut.model_validate(order)
