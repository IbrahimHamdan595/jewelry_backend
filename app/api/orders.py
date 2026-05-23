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
from app.core.ledger import (
    EVENT_ORDER_VOID,
    EVENT_SALE_COIN,
    EVENT_SALE_OUNCE,
    EVENT_SALE_PRODUCT,
    record,
)
from app.core.permissions import require_admin
from app.core.pricing import calculate_price, calculate_unit_price, generate_order_number
from app.deps import get_current_user, get_db
from app.models import (
    CoinType,
    Karat,
    Order,
    OrderItem,
    OrderItemKind,
    OrderStatus,
    OunceType,
    PaymentMethod,
    Product,
    ProductStatus,
    Settings,
    User,
)
from app.schemas.order import (
    CheckoutRequest, OrderItemIn, OrderListOut, OrderOut, OrderSummaryOut, VoidRequest,
)

COIN_OUNCE_QTY_CAP = 100  # D6

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
    q = select(Order).options(selectinload(Order.cashier), selectinload(Order.items))

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


async def _checkout_product_line(
    db: AsyncSession,
    line: OrderItemIn,
    rate_24k: Decimal,
    settings: Settings,
    ledger_records: list[dict],
) -> tuple[list[OrderItem], Decimal]:
    """Atomic product line → N OrderItem rows (one per piece, per D11)."""
    if not line.product_id:
        raise HTTPException(status_code=400, detail="PRODUCT line requires product_id")
    if line.quantity < 1:
        raise HTTPException(status_code=400, detail="quantity must be >= 1")

    product = (
        await db.execute(
            select(Product).where(Product.id == line.product_id).with_for_update()
        )
    ).scalar_one_or_none()
    if not product or not product.is_active:
        raise HTTPException(status_code=400, detail=f"Invalid product {line.product_id}")
    if product.status != ProductStatus.AVAILABLE:
        raise HTTPException(
            status_code=409,
            detail=f"Product {product.code} is not available (status={product.status.value})",
        )
    # Atomic products: quantity > 1 means N copies of the same SKU — physically
    # impossible for a 1-of-1 piece, but the existing behavior loops anyway.
    # We honor it but the same product row gets SOLD on the first iteration.
    if line.quantity > 1:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Atomic products are 1-of-1; quantity must be 1 for {product.code}. "
                "Add the piece as a single line item."
            ),
        )

    markup_map = {
        Karat.K18: settings.markup_k18,
        Karat.K21: settings.markup_k21,
        Karat.K24: settings.markup_k24,
    }
    karat_markup = markup_map.get(product.karat, Decimal("0"))

    priced = calculate_price(
        rate_24k=rate_24k,
        karat=product.karat,
        weight_grams=product.weight_grams,
        margin_percent=product.margin_percent,
        making_charge=product.making_charge,
        karat_markup=karat_markup,
    )

    item = OrderItem(
        item_kind=OrderItemKind.PRODUCT,
        product_id=product.id,
        quantity=1,
        product_code=product.code,
        product_name=product.name_en,
        karat=product.karat,
        weight_grams=product.weight_grams,
        gold_rate_at_sale=rate_24k,
        margin_percent=product.margin_percent,
        making_charge=product.making_charge,
        final_price=priced["final_price"],
    )
    product.status = ProductStatus.SOLD

    ledger_records.append({
        "event_type": EVENT_SALE_PRODUCT,
        "ref_type": "product",
        "ref_id": product.id,
        "payload": {
            "product_code": product.code,
            "karat": product.karat.value,
            "weight_grams": str(product.weight_grams),
            "final_price": str(priced["final_price"]),
            "gold_rate_at_sale": str(rate_24k),
            "status_after": ProductStatus.SOLD.value,
        },
    })
    return [item], priced["final_price"]


async def _checkout_unit_line(
    db: AsyncSession,
    line: OrderItemIn,
    rate_24k: Decimal,
) -> tuple[list[OrderItem], Decimal, dict]:
    """COIN or OUNCE line. Returns (items, line_subtotal, ledger_payload)."""
    is_coin = line.item_kind == "COIN"
    if is_coin:
        type_id = line.coin_type_id
        Model = CoinType
        kind_label = "coin_type"
    else:
        type_id = line.ounce_type_id
        Model = OunceType
        kind_label = "ounce_type"

    if not type_id:
        raise HTTPException(
            status_code=400,
            detail=f"{line.item_kind} line requires {'coin_type_id' if is_coin else 'ounce_type_id'}",
        )
    if line.quantity < 1:
        raise HTTPException(status_code=400, detail="quantity must be >= 1")
    if line.quantity > COIN_OUNCE_QTY_CAP:
        raise HTTPException(
            status_code=422,
            detail=f"quantity must be <= {COIN_OUNCE_QTY_CAP} per line",
        )

    row = (
        await db.execute(
            select(Model).where(Model.id == type_id).with_for_update()
        )
    ).scalar_one_or_none()
    if not row or not row.is_active:
        raise HTTPException(status_code=400, detail=f"Invalid {kind_label} {type_id}")
    if row.on_hand_qty < line.quantity:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Insufficient stock for {row.code}: "
                f"requested {line.quantity}, on hand {row.on_hand_qty}"
            ),
        )

    priced = calculate_unit_price(
        rate_24k=rate_24k,
        weight_grams=row.weight_grams,
        markup_per_gram=row.markup_per_gram,
        margin_mode=row.margin_mode.value,
        margin_value=row.margin_value,
    )

    # One OrderItem row per line (quantity is captured in the column).
    item = OrderItem(
        item_kind=OrderItemKind.COIN if is_coin else OrderItemKind.OUNCE,
        coin_type_id=row.id if is_coin else None,
        ounce_type_id=row.id if not is_coin else None,
        quantity=line.quantity,
        product_code=row.code,
        product_name=row.name_en,
        karat=row.karat,
        weight_grams=row.weight_grams,
        gold_rate_at_sale=rate_24k,
        margin_percent=row.margin_value if row.margin_mode.value == "PERCENT" else Decimal("0"),
        making_charge=Decimal("0"),
        final_price=priced["final_price"] * line.quantity,
    )
    row.on_hand_qty = row.on_hand_qty - line.quantity

    ledger_payload = {
        "event_type": EVENT_SALE_COIN if is_coin else EVENT_SALE_OUNCE,
        "ref_type": kind_label,
        "ref_id": row.id,
        "payload": {
            "code": row.code,
            "quantity": line.quantity,
            "unit_price": str(priced["final_price"]),
            "line_total": str(priced["final_price"] * line.quantity),
            "gold_rate_at_sale": str(rate_24k),
            "qty_after": row.on_hand_qty,
        },
    }
    return [item], priced["final_price"] * line.quantity, ledger_payload


@router.post("", response_model=OrderOut, status_code=201)
async def create_order(
    payload: CheckoutRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    settings = (await db.execute(select(Settings).where(Settings.id == "singleton"))).scalar_one_or_none()
    if not settings:
        raise HTTPException(status_code=500, detail="Settings not configured")
    if not payload.items:
        raise HTTPException(status_code=400, detail="Order must have at least one item")

    rate_info = await get_current_gold_rate(db)
    rate_24k = Decimal(str(rate_info["rate"]))
    order_number = await generate_order_number(db, datetime.now(timezone.utc))

    order_items: list[OrderItem] = []
    subtotal = Decimal(0)
    ledger_records: list[dict] = []

    for line in payload.items:
        if line.item_kind == "PRODUCT":
            items, line_total = await _checkout_product_line(
                db, line, rate_24k, settings, ledger_records
            )
            order_items.extend(items)
            subtotal += line_total
        elif line.item_kind in ("COIN", "OUNCE"):
            items, line_total, payload_dict = await _checkout_unit_line(db, line, rate_24k)
            order_items.extend(items)
            subtotal += line_total
            ledger_records.append(payload_dict)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown item_kind '{line.item_kind}'",
            )

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
    await db.flush()

    for rec in ledger_records:
        rec["payload"]["order_id"] = order.id
        rec["payload"]["order_number"] = order.order_number
        await record(
            db,
            event_type=rec["event_type"],
            actor_user_id=user.id,
            ref_type=rec["ref_type"],
            ref_id=rec["ref_id"],
            payload=rec["payload"],
        )

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

    # Reverse stock per line so the merchandise becomes sellable again.
    reversal_payloads: list[dict] = []
    for item in order.items:
        if item.item_kind == OrderItemKind.PRODUCT and item.product_id:
            product = (
                await db.execute(
                    select(Product).where(Product.id == item.product_id).with_for_update()
                )
            ).scalar_one_or_none()
            if product and product.status == ProductStatus.SOLD:
                product.status = ProductStatus.AVAILABLE
                reversal_payloads.append({
                    "ref_type": "product",
                    "ref_id": product.id,
                    "payload": {
                        "product_code": product.code,
                        "reversal_of": "SALE_PRODUCT",
                        "status_after": ProductStatus.AVAILABLE.value,
                    },
                })
        elif item.item_kind == OrderItemKind.COIN and item.coin_type_id:
            coin = (
                await db.execute(
                    select(CoinType).where(CoinType.id == item.coin_type_id).with_for_update()
                )
            ).scalar_one_or_none()
            if coin:
                coin.on_hand_qty = coin.on_hand_qty + item.quantity
                reversal_payloads.append({
                    "ref_type": "coin_type",
                    "ref_id": coin.id,
                    "payload": {
                        "code": coin.code,
                        "reversal_of": "SALE_COIN",
                        "quantity_returned": item.quantity,
                        "qty_after": coin.on_hand_qty,
                    },
                })
        elif item.item_kind == OrderItemKind.OUNCE and item.ounce_type_id:
            bar = (
                await db.execute(
                    select(OunceType).where(OunceType.id == item.ounce_type_id).with_for_update()
                )
            ).scalar_one_or_none()
            if bar:
                bar.on_hand_qty = bar.on_hand_qty + item.quantity
                reversal_payloads.append({
                    "ref_type": "ounce_type",
                    "ref_id": bar.id,
                    "payload": {
                        "code": bar.code,
                        "reversal_of": "SALE_OUNCE",
                        "quantity_returned": item.quantity,
                        "qty_after": bar.on_hand_qty,
                    },
                })

    order.status = OrderStatus.VOIDED
    order.voided_at = datetime.now(timezone.utc)
    order.voided_by = user.id
    order.void_reason = body.reason

    await db.flush()
    # One umbrella event for the void, plus the per-line reversals.
    await record(
        db,
        event_type=EVENT_ORDER_VOID,
        actor_user_id=user.id,
        ref_type="order",
        ref_id=order.id,
        payload={
            "order_number": order.order_number,
            "reason": body.reason,
            "items_reversed": len(reversal_payloads),
        },
    )
    for rp in reversal_payloads:
        rp["payload"]["voided_order_id"] = order.id
        rp["payload"]["voided_order_number"] = order.order_number
        await record(
            db,
            event_type=EVENT_ORDER_VOID,
            actor_user_id=user.id,
            ref_type=rp["ref_type"],
            ref_id=rp["ref_id"],
            payload=rp["payload"],
        )

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
