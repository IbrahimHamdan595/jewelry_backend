"""Suppliers, supplier purchases (with linked stock-in + stock-out),
repayments, and the store-wide accounts-payable view. Admin-only."""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import gl_postings
from app.core.inventory import consume_from_lot
from app.core.ledger import (
    EVENT_LOT_CREATED,
    EVENT_SUPPLIER_BALANCE_CHANGED,
    EVENT_SUPPLIER_CREATED,
    EVENT_SUPPLIER_PAYMENT_CASH,
    EVENT_SUPPLIER_PAYMENT_GOLD,
    EVENT_SUPPLIER_PURCHASE,
    EVENT_SUPPLIER_UPDATED,
    record,
)
from app.core.daterange import parse_calendar_filter
from app.core.permissions import require_admin
from app.core.pricing import generate_item_code
from app.core.receipt import build_supplier_receipt
from app.schemas.receipt import ReceiptOut
from app.core.supplier_balance import adjust_balance, get_supplier_balances
from app.deps import get_db
from app.models import (
    CoinType,
    DebtUnit,
    GoldLot,
    Karat,
    LotSource,
    OunceType,
    Product,
    ProductStatus,
    Settings,
    Supplier,
    SupplierBalance,
    SupplierItemKind,
    SupplierPayment,
    SupplierPurchase,
    SupplierPurchaseItem,
    SupplierPurchaseMode,
    User,
)
from app.schemas.supplier import (
    AccountsPayableOut,
    APSupplierRow,
    BalanceOut,
    GoldPaymentIn,
    PaymentCreate,
    PaymentOut,
    PurchaseCreate,
    PurchaseItemIn,
    PurchaseListItemOut,
    PurchaseListOut,
    PurchaseOut,
    SupplierCreate,
    SupplierDetailOut,
    SupplierListOut,
    SupplierOut,
    SupplierUpdate,
)

router = APIRouter(prefix="/suppliers", tags=["suppliers"])
ap_router = APIRouter(prefix="/accounts-payable", tags=["accounts-payable"])


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_karat(value: str) -> Karat:
    try:
        return Karat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid karat '{value}'")


def _balance_to_out(row: SupplierBalance) -> BalanceOut:
    return BalanceOut(
        unit=row.unit.value,
        karat=row.karat if row.karat else None,
        balance=row.balance,
    )


# ── Supplier CRUD ─────────────────────────────────────────────────────────────


@router.get("", response_model=SupplierListOut)
async def list_suppliers(
    search: str = "",
    is_active: bool | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    q = select(Supplier)
    if search:
        q = q.where(Supplier.name.ilike(f"%{search}%"))
    if is_active is not None:
        q = q.where(Supplier.is_active.is_(is_active))

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    q = q.order_by(Supplier.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(q)).scalars().all()
    return SupplierListOut(
        items=[SupplierOut.model_validate(r) for r in rows],
        total=total, page=page, page_size=page_size,
    )


@router.post("", response_model=SupplierOut, status_code=201)
async def create_supplier(
    body: SupplierCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    sup = Supplier(**body.model_dump())
    db.add(sup)
    await db.flush()
    await record(
        db,
        event_type=EVENT_SUPPLIER_CREATED,
        actor_user_id=user.id,
        ref_type="supplier",
        ref_id=sup.id,
        payload={"name": sup.name},
    )
    await db.commit()
    await db.refresh(sup)
    return SupplierOut.model_validate(sup)


@router.patch("/{supplier_id}", response_model=SupplierOut)
async def update_supplier(
    supplier_id: str,
    body: SupplierUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    sup = (await db.execute(select(Supplier).where(Supplier.id == supplier_id))).scalar_one_or_none()
    if not sup:
        raise HTTPException(status_code=404, detail="Supplier not found")

    updates = body.model_dump(exclude_unset=True)
    # Deactivation guard: cannot deactivate a supplier with outstanding balances.
    if updates.get("is_active") is False and sup.is_active:
        balances = await get_supplier_balances(db, supplier_id)
        if balances:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot deactivate supplier with {len(balances)} non-zero balance(s). "
                    "Settle outstanding debt first."
                ),
            )
    for field, value in updates.items():
        setattr(sup, field, value)
    await db.flush()
    await record(
        db,
        event_type=EVENT_SUPPLIER_UPDATED,
        actor_user_id=user.id,
        ref_type="supplier",
        ref_id=sup.id,
        payload={"changed": {k: str(v) for k, v in updates.items()}},
    )
    await db.commit()
    await db.refresh(sup)
    return SupplierOut.model_validate(sup)


@router.delete("/{supplier_id}", status_code=204)
async def delete_supplier(
    supplier_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    sup = (await db.execute(select(Supplier).where(Supplier.id == supplier_id))).scalar_one_or_none()
    if not sup:
        raise HTTPException(status_code=404, detail="Supplier not found")
    balances = await get_supplier_balances(db, supplier_id)
    if balances:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete supplier with {len(balances)} non-zero balance(s)",
        )
    sup.is_active = False
    await db.flush()
    await record(
        db,
        event_type=EVENT_SUPPLIER_UPDATED,
        actor_user_id=user.id,
        ref_type="supplier",
        ref_id=sup.id,
        payload={"soft_delete": True},
    )
    await db.commit()


@router.get("/{supplier_id}", response_model=SupplierDetailOut)
async def get_supplier(
    supplier_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    sup = (await db.execute(select(Supplier).where(Supplier.id == supplier_id))).scalar_one_or_none()
    if not sup:
        raise HTTPException(status_code=404, detail="Supplier not found")

    from sqlalchemy.orm import selectinload

    balances = await get_supplier_balances(db, supplier_id)
    purchases = (
        await db.execute(
            select(SupplierPurchase)
            .options(selectinload(SupplierPurchase.items))
            .where(SupplierPurchase.supplier_id == supplier_id)
            .order_by(SupplierPurchase.occurred_at.desc())
        )
    ).scalars().all()
    payments = (
        await db.execute(
            select(SupplierPayment)
            .where(SupplierPayment.supplier_id == supplier_id)
            .order_by(SupplierPayment.paid_at.desc())
        )
    ).scalars().all()

    return SupplierDetailOut(
        supplier=SupplierOut.model_validate(sup),
        balances=[_balance_to_out(b) for b in balances],
        purchases=[PurchaseOut.model_validate(p) for p in purchases],
        payments=[PaymentOut.model_validate(p) for p in payments],
    )


# ── Purchase creation ─────────────────────────────────────────────────────────


async def _materialize_item(
    db: AsyncSession,
    user: User,
    purchase: SupplierPurchase,
    item_in: PurchaseItemIn,
) -> SupplierPurchaseItem:
    """Create the inventory side-effects for one line and return the item row."""
    try:
        kind = SupplierItemKind(item_in.item_kind)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid item_kind '{item_in.item_kind}'")

    item = SupplierPurchaseItem(
        purchase_id=purchase.id,
        item_kind=kind,
        unit_cost_usd=item_in.unit_cost_usd,
        notes=item_in.notes,
    )

    if kind == SupplierItemKind.PURE_GOLD:
        if item_in.weight_grams is None or not item_in.karat:
            raise HTTPException(
                status_code=422, detail="PURE_GOLD item requires weight_grams and karat"
            )
        karat = _parse_karat(item_in.karat)
        lot = GoldLot(
            karat=karat,
            weight_grams=item_in.weight_grams,
            weight_remaining_grams=item_in.weight_grams,
            source=LotSource.SUPPLIER,
            source_ref_type="supplier_purchase",
            source_ref_id=purchase.id,
            cost_basis_usd=item_in.unit_cost_usd,
            notes=f"From supplier purchase {purchase.id}",
        )
        db.add(lot)
        await db.flush()
        item.lot_id = lot.id
        item.weight_grams = item_in.weight_grams
        item.karat = karat
        await record(
            db,
            event_type=EVENT_LOT_CREATED,
            actor_user_id=user.id,
            ref_type="gold_lot",
            ref_id=lot.id,
            payload={
                "karat": karat.value,
                "weight_grams": str(item_in.weight_grams),
                "source": LotSource.SUPPLIER.value,
                "cost_basis_usd": str(item_in.unit_cost_usd),
                "from_purchase_id": purchase.id,
            },
        )

    elif kind == SupplierItemKind.COIN:
        if not item_in.coin_type_id or not item_in.quantity:
            raise HTTPException(
                status_code=422, detail="COIN item requires coin_type_id and quantity"
            )
        coin = (
            await db.execute(
                select(CoinType).where(CoinType.id == item_in.coin_type_id).with_for_update()
            )
        ).scalar_one_or_none()
        if not coin:
            raise HTTPException(status_code=404, detail=f"Coin type {item_in.coin_type_id} not found")
        coin.on_hand_qty = coin.on_hand_qty + item_in.quantity
        item.coin_type_id = coin.id
        item.quantity = item_in.quantity
        item.karat = coin.karat
        item.weight_grams = coin.weight_grams * item_in.quantity

    elif kind == SupplierItemKind.OUNCE:
        if not item_in.ounce_type_id or not item_in.quantity:
            raise HTTPException(
                status_code=422, detail="OUNCE item requires ounce_type_id and quantity"
            )
        bar = (
            await db.execute(
                select(OunceType).where(OunceType.id == item_in.ounce_type_id).with_for_update()
            )
        ).scalar_one_or_none()
        if not bar:
            raise HTTPException(status_code=404, detail=f"Ounce type {item_in.ounce_type_id} not found")
        bar.on_hand_qty = bar.on_hand_qty + item_in.quantity
        item.ounce_type_id = bar.id
        item.quantity = item_in.quantity
        item.karat = bar.karat
        item.weight_grams = bar.weight_grams * item_in.quantity

    elif kind == SupplierItemKind.PRODUCT:
        if not item_in.product:
            raise HTTPException(
                status_code=422, detail="PRODUCT item requires `product` spec"
            )
        spec = item_in.product
        for required in ("name_en", "category", "karat", "weight_grams", "margin_percent", "making_charge"):
            if required not in spec:
                raise HTTPException(
                    status_code=422, detail=f"PRODUCT item `product` missing '{required}'"
                )
        karat = _parse_karat(spec["karat"])
        code = await generate_item_code(db, karat)
        product = Product(
            code=code,
            name_en=spec["name_en"],
            name_ar=spec.get("name_ar", ""),
            category=spec["category"],
            category_id=spec.get("category_id"),
            karat=karat,
            weight_grams=Decimal(str(spec["weight_grams"])),
            margin_percent=Decimal(str(spec["margin_percent"])),
            making_charge=Decimal(str(spec["making_charge"])),
            photos=spec.get("photos", []),
            is_used=False,
            cost_basis_usd=item_in.unit_cost_usd,
            status=ProductStatus.AVAILABLE,
            source_ref_type="supplier_purchase",
            source_ref_id=purchase.id,
        )
        db.add(product)
        await db.flush()
        item.product_id = product.id
        item.karat = karat
        item.weight_grams = Decimal(str(spec["weight_grams"]))

    db.add(item)
    await db.flush()
    return item


async def _consume_lots(
    db: AsyncSession,
    user: User,
    gold_payments: list[GoldPaymentIn],
    ref_type: str,
    ref_id: str,
) -> dict[str, Decimal]:
    """Consume `grams` off each lot; verify karat matches; return grams-per-karat total."""
    total_by_karat: dict[str, Decimal] = {}
    for gp in gold_payments:
        karat = _parse_karat(gp.karat)
        lot = (
            await db.execute(select(GoldLot).where(GoldLot.id == gp.lot_id))
        ).scalar_one_or_none()
        if not lot:
            raise HTTPException(status_code=404, detail=f"Lot {gp.lot_id} not found")
        if lot.karat != karat:
            raise HTTPException(
                status_code=422,
                detail=f"Lot {gp.lot_id} is {lot.karat.value}, payment claims {karat.value}",
            )
        await consume_from_lot(
            db,
            lot_id=gp.lot_id,
            grams=gp.grams,
            ref_type=ref_type,
            ref_id=ref_id,
            actor_user_id=user.id,
        )
        total_by_karat[karat.value] = total_by_karat.get(karat.value, Decimal("0")) + gp.grams
    return total_by_karat


@router.post("/{supplier_id}/purchases", response_model=PurchaseOut, status_code=201)
async def create_purchase(
    supplier_id: str,
    body: PurchaseCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    sup = (await db.execute(select(Supplier).where(Supplier.id == supplier_id))).scalar_one_or_none()
    if not sup or not sup.is_active:
        raise HTTPException(status_code=404, detail="Supplier not found or inactive")

    try:
        mode = SupplierPurchaseMode(body.payment_mode)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid payment_mode '{body.payment_mode}'")

    # Sanity: cash_paid <= total_cash_due.
    if body.cash_paid_at_creation > body.total_cash_due:
        raise HTTPException(
            status_code=422,
            detail=(
                f"cash_paid_at_creation ({body.cash_paid_at_creation}) "
                f"exceeds total_cash_due ({body.total_cash_due})"
            ),
        )
    if mode == SupplierPurchaseMode.CASH:
        if body.gold_payments_at_creation or body.total_grams_due_by_karat:
            raise HTTPException(
                status_code=422,
                detail="CASH payment_mode rejects any gold payments or gold dues",
            )
    if mode == SupplierPurchaseMode.GOLD:
        if body.cash_paid_at_creation != 0 or body.total_cash_due != 0:
            raise HTTPException(
                status_code=422,
                detail="GOLD payment_mode rejects any cash dues or cash payments",
            )

    # Normalize the JSON dict keys/values.
    total_grams_due = {k: Decimal(str(v)) for k, v in body.total_grams_due_by_karat.items()}
    for k_str in total_grams_due:
        _parse_karat(k_str)  # validates karat strings

    # Create the purchase row first so items can FK-reference it.
    purchase = SupplierPurchase(
        supplier_id=sup.id,
        payment_mode=mode,
        trade_markup_per_gram=body.trade_markup_per_gram,
        total_cash_due=body.total_cash_due,
        total_grams_due_by_karat={k: str(v) for k, v in total_grams_due.items()},
        cash_paid_at_creation=body.cash_paid_at_creation,
        grams_paid_at_creation_by_karat={},  # filled after lot consumption
        notes=body.notes,
        created_by_user_id=user.id,
    )
    if body.occurred_at:
        purchase.occurred_at = body.occurred_at
    db.add(purchase)
    await db.flush()

    # Consume lots for gold paid at creation.
    grams_paid = await _consume_lots(
        db, user, body.gold_payments_at_creation,
        ref_type="supplier_purchase", ref_id=purchase.id,
    )
    # Verify grams_paid does not exceed total_grams_due per karat (overpayment).
    for k, paid in grams_paid.items():
        due = total_grams_due.get(k, Decimal("0"))
        if paid > due:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Gold paid {paid} K{k} exceeds total due {due} K{k} on this purchase"
                ),
            )
    purchase.grams_paid_at_creation_by_karat = {k: str(v) for k, v in grams_paid.items()}

    # Materialize items (stock-in).
    for item_in in body.items:
        await _materialize_item(db, user, purchase, item_in)

    # Compute outstanding deltas.
    cash_owed_delta = body.total_cash_due - body.cash_paid_at_creation
    gold_owed_deltas: dict[str, Decimal] = {}
    for k, due in total_grams_due.items():
        paid = grams_paid.get(k, Decimal("0"))
        gold_owed_deltas[k] = due - paid

    # Adjust supplier_balances. Add positive deltas (we now owe). Adjust ledger.
    if cash_owed_delta > 0:
        before, after = await adjust_balance(
            db, supplier_id=sup.id, unit=DebtUnit.CASH, karat="", delta=cash_owed_delta,
        )
        await record(
            db,
            event_type=EVENT_SUPPLIER_BALANCE_CHANGED,
            actor_user_id=user.id,
            ref_type="supplier_balance",
            ref_id=f"{sup.id}:CASH:",
            payload={
                "supplier_id": sup.id, "unit": "CASH", "karat": None,
                "delta": str(cash_owed_delta),
                "before": str(before), "after": str(after),
                "reason": "purchase",
                "purchase_id": purchase.id,
            },
        )
    for k, delta in gold_owed_deltas.items():
        if delta > 0:
            before, after = await adjust_balance(
                db, supplier_id=sup.id, unit=DebtUnit.GOLD, karat=k, delta=delta,
            )
            await record(
                db,
                event_type=EVENT_SUPPLIER_BALANCE_CHANGED,
                actor_user_id=user.id,
                ref_type="supplier_balance",
                ref_id=f"{sup.id}:GOLD:{k}",
                payload={
                    "supplier_id": sup.id, "unit": "GOLD", "karat": k,
                    "delta": str(delta),
                    "before": str(before), "after": str(after),
                    "reason": "purchase",
                    "purchase_id": purchase.id,
                },
            )

    await record(
        db,
        event_type=EVENT_SUPPLIER_PURCHASE,
        actor_user_id=user.id,
        ref_type="supplier_purchase",
        ref_id=purchase.id,
        payload={
            "supplier_id": sup.id,
            "supplier_name": sup.name,
            "payment_mode": mode.value,
            "total_cash_due": str(body.total_cash_due),
            "total_grams_due_by_karat": {k: str(v) for k, v in total_grams_due.items()},
            "cash_paid_at_creation": str(body.cash_paid_at_creation),
            "grams_paid_at_creation_by_karat": {k: str(v) for k, v in grams_paid.items()},
            "trade_markup_per_gram": str(body.trade_markup_per_gram) if body.trade_markup_per_gram else None,
            "items": len(body.items),
        },
    )

    # Module 1 auto-posting (no-op unless the flag is ON).
    _settings = (await db.execute(select(Settings).where(Settings.id == "singleton"))).scalar_one_or_none()
    if _settings:
        await gl_postings.post_supplier_purchase(db, purchase, _settings, user.id)

    await db.commit()

    # Re-fetch with items loaded.
    from sqlalchemy.orm import selectinload
    purchase = (
        await db.execute(
            select(SupplierPurchase)
            .options(selectinload(SupplierPurchase.items))
            .where(SupplierPurchase.id == purchase.id)
        )
    ).scalar_one()
    return PurchaseOut.model_validate(purchase)


@router.get("/purchases/list", response_model=PurchaseListOut)
async def list_purchases(
    granularity: str = "",
    date: str = "",
    supplier_id: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Store-wide supplier purchases for the unified orders page (Phase 5),
    with Beirut-local calendar filtering."""
    from sqlalchemy.orm import selectinload

    q = select(SupplierPurchase).options(selectinload(SupplierPurchase.items))
    if supplier_id:
        q = q.where(SupplierPurchase.supplier_id == supplier_id)
    cal_range = parse_calendar_filter(granularity, date)
    if cal_range:
        start, end = cal_range
        q = q.where(SupplierPurchase.occurred_at >= start, SupplierPurchase.occurred_at < end)

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    q = q.order_by(SupplierPurchase.occurred_at.desc()).offset((page - 1) * page_size).limit(page_size)
    purchases = (await db.execute(q)).scalars().all()

    supplier_ids = {p.supplier_id for p in purchases}
    names: dict[str, str] = {}
    if supplier_ids:
        for s in (await db.execute(select(Supplier).where(Supplier.id.in_(supplier_ids)))).scalars():
            names[s.id] = s.name

    return PurchaseListOut(
        items=[
            PurchaseListItemOut(
                id=p.id,
                supplier_id=p.supplier_id,
                supplier_name=names.get(p.supplier_id, "—"),
                occurred_at=p.occurred_at,
                payment_mode=p.payment_mode.value,
                total_cash_due=p.total_cash_due,
                total_grams_due_by_karat=p.total_grams_due_by_karat,
                item_count=len(p.items),
                notes=p.notes,
            )
            for p in purchases
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/purchases/{purchase_id}/receipt", response_model=ReceiptOut)
async def get_purchase_receipt(
    purchase_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Normalized supplier-purchase receipt for printing (Phase 4)."""
    from sqlalchemy.orm import selectinload

    purchase = (
        await db.execute(
            select(SupplierPurchase)
            .options(selectinload(SupplierPurchase.items))
            .where(SupplierPurchase.id == purchase_id)
        )
    ).scalar_one_or_none()
    if not purchase:
        raise HTTPException(status_code=404, detail="Purchase not found")

    settings = (
        await db.execute(select(Settings).where(Settings.id == "singleton"))
    ).scalar_one_or_none()
    if not settings:
        raise HTTPException(status_code=500, detail="Settings not configured")

    supplier = (
        await db.execute(select(Supplier).where(Supplier.id == purchase.supplier_id))
    ).scalar_one_or_none()
    cashier = (
        await db.execute(select(User).where(User.id == purchase.created_by_user_id))
    ).scalar_one_or_none()

    # Resolve per-item human descriptions from the referenced product/coin/ounce.
    product_ids = {i.product_id for i in purchase.items if i.product_id}
    coin_ids = {i.coin_type_id for i in purchase.items if i.coin_type_id}
    ounce_ids = {i.ounce_type_id for i in purchase.items if i.ounce_type_id}
    names: dict[str, str] = {}
    if product_ids:
        for p in (await db.execute(select(Product).where(Product.id.in_(product_ids)))).scalars():
            names[p.id] = p.name_en
    if coin_ids:
        for c in (await db.execute(select(CoinType).where(CoinType.id.in_(coin_ids)))).scalars():
            names[c.id] = c.name_en
    if ounce_ids:
        for o in (await db.execute(select(OunceType).where(OunceType.id.in_(ounce_ids)))).scalars():
            names[o.id] = o.name_en

    item_descriptions: dict[str, str] = {}
    for it in purchase.items:
        ref = it.product_id or it.coin_type_id or it.ounce_type_id
        if ref and ref in names:
            item_descriptions[it.id] = names[ref]

    return build_supplier_receipt(
        purchase, settings,
        supplier_name=supplier.name if supplier else None,
        cashier_name=cashier.name if cashier else None,
        item_descriptions=item_descriptions,
    )


# ── Repayment ─────────────────────────────────────────────────────────────────


@router.post("/{supplier_id}/payments", response_model=PaymentOut, status_code=201)
async def create_payment(
    supplier_id: str,
    body: PaymentCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    sup = (await db.execute(select(Supplier).where(Supplier.id == supplier_id))).scalar_one_or_none()
    if not sup:
        raise HTTPException(status_code=404, detail="Supplier not found")

    try:
        unit = DebtUnit(body.unit)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid unit '{body.unit}'")

    payment = SupplierPayment(
        supplier_id=sup.id,
        unit=unit,
        amount=body.amount,
        paid_by_user_id=user.id,
        notes=body.notes,
    )

    if unit == DebtUnit.CASH:
        if body.karat:
            raise HTTPException(status_code=422, detail="CASH payment must not specify karat")
        if body.gold_payments:
            raise HTTPException(status_code=422, detail="CASH payment must not specify gold_payments")
        db.add(payment)
        await db.flush()
        before, after = await adjust_balance(
            db, supplier_id=sup.id, unit=DebtUnit.CASH, karat="", delta=-body.amount,
        )
        await record(
            db,
            event_type=EVENT_SUPPLIER_PAYMENT_CASH,
            actor_user_id=user.id,
            ref_type="supplier_payment",
            ref_id=payment.id,
            payload={
                "supplier_id": sup.id,
                "amount": str(body.amount),
                "balance_before": str(before),
                "balance_after": str(after),
                "notes": body.notes,
            },
        )
        await record(
            db,
            event_type=EVENT_SUPPLIER_BALANCE_CHANGED,
            actor_user_id=user.id,
            ref_type="supplier_balance",
            ref_id=f"{sup.id}:CASH:",
            payload={
                "supplier_id": sup.id, "unit": "CASH", "karat": None,
                "delta": str(-body.amount),
                "before": str(before), "after": str(after),
                "reason": "payment", "payment_id": payment.id,
            },
        )

    else:  # GOLD
        if not body.karat:
            raise HTTPException(status_code=422, detail="GOLD payment requires karat")
        karat = _parse_karat(body.karat)
        payment.karat = karat
        if not body.gold_payments:
            raise HTTPException(
                status_code=422, detail="GOLD payment requires gold_payments (which lots to draw)"
            )
        # Verify amount matches sum of grams drawn.
        sum_grams = sum(gp.grams for gp in body.gold_payments)
        if sum_grams != body.amount:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"GOLD payment amount ({body.amount}) must equal sum of grams drawn "
                    f"({sum_grams})"
                ),
            )
        for gp in body.gold_payments:
            if gp.karat != karat.value:
                raise HTTPException(
                    status_code=422,
                    detail=f"All gold_payments must match payment karat {karat.value}",
                )

        db.add(payment)
        await db.flush()

        # Pre-check balance before consuming any lots (avoid partial state).
        before, after = await adjust_balance(
            db, supplier_id=sup.id, unit=DebtUnit.GOLD, karat=karat.value, delta=-body.amount,
        )
        # Now consume lots — failure here aborts the whole transaction.
        consumed = await _consume_lots(
            db, user, body.gold_payments,
            ref_type="supplier_payment", ref_id=payment.id,
        )
        payment.source_lot_ids = [gp.lot_id for gp in body.gold_payments]

        await record(
            db,
            event_type=EVENT_SUPPLIER_PAYMENT_GOLD,
            actor_user_id=user.id,
            ref_type="supplier_payment",
            ref_id=payment.id,
            payload={
                "supplier_id": sup.id,
                "karat": karat.value,
                "amount_grams": str(body.amount),
                "lots_consumed": {k: str(v) for k, v in consumed.items()},
                "balance_before": str(before),
                "balance_after": str(after),
                "notes": body.notes,
            },
        )
        await record(
            db,
            event_type=EVENT_SUPPLIER_BALANCE_CHANGED,
            actor_user_id=user.id,
            ref_type="supplier_balance",
            ref_id=f"{sup.id}:GOLD:{karat.value}",
            payload={
                "supplier_id": sup.id, "unit": "GOLD", "karat": karat.value,
                "delta": str(-body.amount),
                "before": str(before), "after": str(after),
                "reason": "payment", "payment_id": payment.id,
            },
        )

    # Module 1 auto-posting (no-op unless the flag is ON).
    _settings = (await db.execute(select(Settings).where(Settings.id == "singleton"))).scalar_one_or_none()
    if _settings:
        await gl_postings.post_supplier_payment(db, payment, _settings, user.id)

    await db.commit()
    await db.refresh(payment)
    return PaymentOut.model_validate(payment)


# ── Accounts payable ──────────────────────────────────────────────────────────


@ap_router.get("", response_model=AccountsPayableOut)
async def accounts_payable(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    rows = (
        await db.execute(
            select(SupplierBalance, Supplier.name)
            .join(Supplier, Supplier.id == SupplierBalance.supplier_id)
            .where(SupplierBalance.balance != 0)
            .order_by(Supplier.name)
        )
    ).all()

    by_supplier: dict[str, APSupplierRow] = {}
    total_cash = Decimal("0")
    total_gold: dict[str, Decimal] = {}

    for balance, supplier_name in rows:
        if balance.unit == DebtUnit.CASH:
            total_cash += balance.balance
        else:
            total_gold[balance.karat] = total_gold.get(balance.karat, Decimal("0")) + balance.balance

        if balance.supplier_id not in by_supplier:
            by_supplier[balance.supplier_id] = APSupplierRow(
                supplier_id=balance.supplier_id,
                supplier_name=supplier_name,
                balances=[],
            )
        by_supplier[balance.supplier_id].balances.append(_balance_to_out(balance))

    return AccountsPayableOut(
        total_cash_owed=total_cash,
        total_grams_owed_by_karat=total_gold,
        suppliers=list(by_supplier.values()),
    )
