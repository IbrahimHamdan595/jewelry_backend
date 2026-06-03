"""Walk-in buyback endpoints — open to cashiers and admins."""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import gl_postings
from app.core.gold_api import get_current_gold_rate
from app.core.ledger import (
    EVENT_BUYBACK_COIN,
    EVENT_BUYBACK_OUNCE,
    EVENT_BUYBACK_PURE_GOLD,
    EVENT_BUYBACK_USED_PRODUCT,
    EVENT_LOT_CREATED,
    record,
)
from app.core.daterange import parse_calendar_filter
from app.core.pricing import compute_buyback_price
from app.deps import get_current_user, get_db
from app.models import (
    BuybackKind,
    BuybackMarginMode,
    BuybackPriceMode,
    CoinType,
    GoldLot,
    Karat,
    LotSource,
    OunceType,
    Product,
    Settings,
    User,
    WalkinBuyback,
)
from app.core.receipt import build_buyback_receipt
from app.schemas.buyback import (
    BuybackCreate, BuybackListOut, BuybackQuoteOut, BuybackReceiptOut,
)
from app.schemas.receipt import ReceiptOut

router = APIRouter(prefix="/buybacks", tags=["buybacks"])


async def _load_settings(db: AsyncSession) -> Settings:
    cfg = (await db.execute(select(Settings).where(Settings.id == "singleton"))).scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=500, detail="Settings singleton missing")
    return cfg


def _parse_karat(value: str) -> Karat:
    try:
        return Karat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid karat '{value}'")


def _check_rate_drift(current: Decimal, expected: Decimal, max_pct: Decimal) -> None:
    if expected <= 0:
        return
    drift_pct = abs(current - expected) / expected * Decimal(100)
    if drift_pct > max_pct:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Gold rate drifted {drift_pct:.2f}% since quote "
                f"(quoted {expected}, current {current}, max {max_pct}%). "
                "Re-quote and confirm with seller."
            ),
        )


@router.get("/quote", response_model=BuybackQuoteOut)
async def buyback_quote(
    karat: str,
    weight_grams: Decimal = Query(gt=0),
    margin_mode: str | None = None,
    margin_value: Decimal | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Live buyback quote for a pure-gold walk-in. Cashier shows this on screen."""
    cfg = await _load_settings(db)
    rate_info = await get_current_gold_rate(db)
    rate_24k = Decimal(str(rate_info["rate"]))
    karat_val = _parse_karat(karat)

    mode = margin_mode or cfg.default_buyback_margin_mode.value
    value = margin_value if margin_value is not None else cfg.default_buyback_margin_value

    try:
        priced = compute_buyback_price(
            rate_24k=rate_24k,
            karat=karat_val,
            weight_grams=weight_grams,
            margin_mode=mode,
            margin_value=value,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    return BuybackQuoteOut(
        rate_24k=rate_24k,
        rate_source=rate_info["source"],
        rate_is_stale=bool(rate_info.get("is_stale", False)),
        karat=karat_val.value,
        purity_rate=priced["purity_rate"],
        weight_grams=weight_grams,
        margin_mode=mode,
        margin_value=value,
        effective_rate_per_gram=priced["effective_rate_per_gram"],
        buy_price=priced["buy_price"],
    )


@router.post("", response_model=BuybackReceiptOut, status_code=201)
async def create_buyback(
    body: BuybackCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        kind = BuybackKind(body.kind)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid kind '{body.kind}'")

    cfg = await _load_settings(db)
    rate_info = await get_current_gold_rate(db)
    rate_24k = Decimal(str(rate_info["rate"]))

    # Drift check is keyed on the live rate vs. the quoted rate.
    if body.expected_rate is not None:
        _check_rate_drift(rate_24k, body.expected_rate, cfg.buyback_rate_drift_pct_max)

    # Dispatch to per-kind handler.
    if kind == BuybackKind.PURE_GOLD:
        return await _create_pure_gold_buyback(db, user, body, cfg, rate_24k)
    if kind == BuybackKind.COIN:
        return await _create_coin_buyback(db, user, body, cfg, rate_24k)
    if kind == BuybackKind.OUNCE:
        return await _create_ounce_buyback(db, user, body, cfg, rate_24k)
    if kind == BuybackKind.USED_PRODUCT:
        return await _create_used_product_buyback(db, user, body, rate_24k)
    raise HTTPException(status_code=500, detail="unhandled buyback kind")


# ── Per-kind handlers ─────────────────────────────────────────────────────────


def _resolve_margin(
    body: BuybackCreate, cfg: Settings
) -> tuple[BuybackPriceMode, BuybackMarginMode | None, Decimal | None]:
    """Decide FORMULA vs MANUAL based on whether the operator entered a price."""
    if body.manual_price is not None:
        return BuybackPriceMode.MANUAL, None, None
    mode_str = body.margin_mode or cfg.default_buyback_margin_mode.value
    value = body.margin_value if body.margin_value is not None else cfg.default_buyback_margin_value
    try:
        mode = BuybackMarginMode(mode_str)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid margin_mode '{mode_str}'")
    return BuybackPriceMode.FORMULA, mode, value


async def _create_pure_gold_buyback(
    db: AsyncSession, user: User, body: BuybackCreate, cfg: Settings, rate_24k: Decimal,
) -> BuybackReceiptOut:
    if body.karat is None or body.weight_grams is None:
        raise HTTPException(
            status_code=422,
            detail="PURE_GOLD buyback requires karat and weight_grams",
        )
    karat = _parse_karat(body.karat)
    price_mode, margin_mode, margin_value = _resolve_margin(body, cfg)

    if price_mode == BuybackPriceMode.MANUAL:
        buy_price = body.manual_price  # type: ignore[assignment]
    else:
        try:
            priced = compute_buyback_price(
                rate_24k=rate_24k,
                karat=karat,
                weight_grams=body.weight_grams,
                margin_mode=margin_mode.value,  # type: ignore[union-attr]
                margin_value=margin_value,  # type: ignore[arg-type]
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        buy_price = priced["buy_price"]

    # Create the buyback row first so the lot can FK-reference it.
    buyback = WalkinBuyback(
        seller_name=body.seller_name,
        seller_phone=body.seller_phone,
        cashier_id=user.id,
        kind=BuybackKind.PURE_GOLD,
        weight_grams=body.weight_grams,
        karat=karat,
        buy_price_usd=buy_price,
        gold_rate_at_buy=rate_24k,
        buyback_margin_mode=margin_mode,
        buyback_margin_value=margin_value,
        price_mode=price_mode,
        notes=body.notes,
    )
    db.add(buyback)
    await db.flush()

    lot = GoldLot(
        karat=karat,
        weight_grams=body.weight_grams,
        weight_remaining_grams=body.weight_grams,
        source=LotSource.BUYBACK,
        source_ref_type="walkin_buyback",
        source_ref_id=buyback.id,
        cost_basis_usd=buy_price,
        notes=f"From walk-in buyback {buyback.id}",
    )
    db.add(lot)
    await db.flush()
    buyback.result_lot_id = lot.id

    await record(
        db,
        event_type=EVENT_LOT_CREATED,
        actor_user_id=user.id,
        ref_type="gold_lot",
        ref_id=lot.id,
        payload={
            "karat": karat.value,
            "weight_grams": str(body.weight_grams),
            "source": LotSource.BUYBACK.value,
            "cost_basis_usd": str(buy_price),
            "from_buyback_id": buyback.id,
        },
    )
    await record(
        db,
        event_type=EVENT_BUYBACK_PURE_GOLD,
        actor_user_id=user.id,
        ref_type="walkin_buyback",
        ref_id=buyback.id,
        payload={
            "seller_name": body.seller_name,
            "seller_phone": body.seller_phone,
            "karat": karat.value,
            "weight_grams": str(body.weight_grams),
            "buy_price_usd": str(buy_price),
            "rate_24k": str(rate_24k),
            "price_mode": price_mode.value,
            "result_lot_id": lot.id,
        },
    )

    # Module 1 auto-posting (no-op unless the flag is ON).
    await gl_postings.post_buyback(db, buyback, cfg, user.id)

    await db.commit()
    await db.refresh(buyback)
    return BuybackReceiptOut.model_validate(buyback)


async def _create_coin_buyback(
    db: AsyncSession, user: User, body: BuybackCreate, cfg: Settings, rate_24k: Decimal,
) -> BuybackReceiptOut:
    if not body.coin_type_id or not body.quantity:
        raise HTTPException(
            status_code=422, detail="COIN buyback requires coin_type_id and quantity"
        )
    coin = (
        await db.execute(
            select(CoinType).where(CoinType.id == body.coin_type_id).with_for_update()
        )
    ).scalar_one_or_none()
    if not coin:
        raise HTTPException(status_code=404, detail=f"Coin type {body.coin_type_id} not found")

    price_mode, margin_mode, margin_value = _resolve_margin(body, cfg)
    if price_mode == BuybackPriceMode.MANUAL:
        buy_price = body.manual_price  # type: ignore[assignment]
    else:
        try:
            priced = compute_buyback_price(
                rate_24k=rate_24k,
                karat=coin.karat,
                weight_grams=coin.weight_grams,
                margin_mode=margin_mode.value,  # type: ignore[union-attr]
                margin_value=margin_value,  # type: ignore[arg-type]
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        buy_price = priced["buy_price"] * body.quantity

    coin.on_hand_qty = coin.on_hand_qty + body.quantity

    buyback = WalkinBuyback(
        seller_name=body.seller_name,
        seller_phone=body.seller_phone,
        cashier_id=user.id,
        kind=BuybackKind.COIN,
        coin_type_id=coin.id,
        quantity=body.quantity,
        karat=coin.karat,
        weight_grams=coin.weight_grams * body.quantity,
        buy_price_usd=buy_price,
        gold_rate_at_buy=rate_24k,
        buyback_margin_mode=margin_mode,
        buyback_margin_value=margin_value,
        price_mode=price_mode,
        notes=body.notes,
    )
    db.add(buyback)
    await db.flush()

    await record(
        db,
        event_type=EVENT_BUYBACK_COIN,
        actor_user_id=user.id,
        ref_type="walkin_buyback",
        ref_id=buyback.id,
        payload={
            "seller_name": body.seller_name,
            "seller_phone": body.seller_phone,
            "coin_type_id": coin.id,
            "coin_code": coin.code,
            "quantity": body.quantity,
            "buy_price_usd": str(buy_price),
            "rate_24k": str(rate_24k),
            "price_mode": price_mode.value,
            "qty_after": coin.on_hand_qty,
        },
    )
    # Module 1 auto-posting (no-op unless the flag is ON).
    await gl_postings.post_buyback(db, buyback, cfg, user.id)

    await db.commit()
    await db.refresh(buyback)
    return BuybackReceiptOut.model_validate(buyback)


async def _create_ounce_buyback(
    db: AsyncSession, user: User, body: BuybackCreate, cfg: Settings, rate_24k: Decimal,
) -> BuybackReceiptOut:
    if not body.ounce_type_id or not body.quantity:
        raise HTTPException(
            status_code=422, detail="OUNCE buyback requires ounce_type_id and quantity"
        )
    bar = (
        await db.execute(
            select(OunceType).where(OunceType.id == body.ounce_type_id).with_for_update()
        )
    ).scalar_one_or_none()
    if not bar:
        raise HTTPException(status_code=404, detail=f"Ounce type {body.ounce_type_id} not found")

    price_mode, margin_mode, margin_value = _resolve_margin(body, cfg)
    if price_mode == BuybackPriceMode.MANUAL:
        buy_price = body.manual_price  # type: ignore[assignment]
    else:
        try:
            priced = compute_buyback_price(
                rate_24k=rate_24k,
                karat=bar.karat,
                weight_grams=bar.weight_grams,
                margin_mode=margin_mode.value,  # type: ignore[union-attr]
                margin_value=margin_value,  # type: ignore[arg-type]
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        buy_price = priced["buy_price"] * body.quantity

    bar.on_hand_qty = bar.on_hand_qty + body.quantity

    buyback = WalkinBuyback(
        seller_name=body.seller_name,
        seller_phone=body.seller_phone,
        cashier_id=user.id,
        kind=BuybackKind.OUNCE,
        ounce_type_id=bar.id,
        quantity=body.quantity,
        karat=bar.karat,
        weight_grams=bar.weight_grams * body.quantity,
        buy_price_usd=buy_price,
        gold_rate_at_buy=rate_24k,
        buyback_margin_mode=margin_mode,
        buyback_margin_value=margin_value,
        price_mode=price_mode,
        notes=body.notes,
    )
    db.add(buyback)
    await db.flush()

    await record(
        db,
        event_type=EVENT_BUYBACK_OUNCE,
        actor_user_id=user.id,
        ref_type="walkin_buyback",
        ref_id=buyback.id,
        payload={
            "seller_name": body.seller_name,
            "seller_phone": body.seller_phone,
            "ounce_type_id": bar.id,
            "ounce_code": bar.code,
            "quantity": body.quantity,
            "buy_price_usd": str(buy_price),
            "rate_24k": str(rate_24k),
            "price_mode": price_mode.value,
            "qty_after": bar.on_hand_qty,
        },
    )
    # Module 1 auto-posting (no-op unless the flag is ON).
    await gl_postings.post_buyback(db, buyback, cfg, user.id)

    await db.commit()
    await db.refresh(buyback)
    return BuybackReceiptOut.model_validate(buyback)


async def _create_used_product_buyback(
    db: AsyncSession, user: User, body: BuybackCreate, rate_24k: Decimal,
) -> BuybackReceiptOut:
    """USED_PRODUCT buyback in Phase 3: persist the row only.

    The actual Product is materialised by the polish endpoint in Phase 6,
    or rejected/melted at admin discretion. No stock change happens here.
    """
    if body.karat is None or body.weight_grams is None or body.manual_price is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "USED_PRODUCT buyback requires karat, weight_grams, and manual_price. "
                "Used pieces are priced by hand."
            ),
        )
    karat = _parse_karat(body.karat)

    buyback = WalkinBuyback(
        seller_name=body.seller_name,
        seller_phone=body.seller_phone,
        cashier_id=user.id,
        kind=BuybackKind.USED_PRODUCT,
        karat=karat,
        weight_grams=body.weight_grams,
        buy_price_usd=body.manual_price,
        gold_rate_at_buy=rate_24k,
        buyback_margin_mode=None,
        buyback_margin_value=None,
        price_mode=BuybackPriceMode.MANUAL,
        notes=body.notes,
    )
    db.add(buyback)
    await db.flush()

    await record(
        db,
        event_type=EVENT_BUYBACK_USED_PRODUCT,
        actor_user_id=user.id,
        ref_type="walkin_buyback",
        ref_id=buyback.id,
        payload={
            "seller_name": body.seller_name,
            "seller_phone": body.seller_phone,
            "karat": karat.value,
            "weight_grams": str(body.weight_grams),
            "buy_price_usd": str(body.manual_price),
            "rate_24k": str(rate_24k),
            "pending_polish": True,
        },
    )
    # Module 1 auto-posting (no-op unless the flag is ON).
    await gl_postings.post_buyback(db, buyback, cfg, user.id)

    await db.commit()
    await db.refresh(buyback)
    return BuybackReceiptOut.model_validate(buyback)


# ── Read endpoints ────────────────────────────────────────────────────────────


@router.get("", response_model=BuybackListOut)
async def list_buybacks(
    kind: str = "",
    cashier_id: str = "",
    granularity: str = "",
    date: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = select(WalkinBuyback)
    if kind:
        try:
            q = q.where(WalkinBuyback.kind == BuybackKind(kind))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid kind '{kind}'")
    if cashier_id:
        q = q.where(WalkinBuyback.cashier_id == cashier_id)
    # Phase 5 — calendar filter (Beirut-local day/month/year).
    cal_range = parse_calendar_filter(granularity, date)
    if cal_range:
        start, end = cal_range
        q = q.where(WalkinBuyback.occurred_at >= start, WalkinBuyback.occurred_at < end)

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    q = q.order_by(WalkinBuyback.occurred_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(q)).scalars().all()
    return BuybackListOut(
        items=[BuybackReceiptOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{buyback_id}", response_model=BuybackReceiptOut)
async def get_buyback(
    buyback_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    row = (
        await db.execute(select(WalkinBuyback).where(WalkinBuyback.id == buyback_id))
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Buyback not found")
    return BuybackReceiptOut.model_validate(row)


@router.get("/{buyback_id}/receipt", response_model=ReceiptOut)
async def get_buyback_receipt(
    buyback_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Normalized buyback receipt for printing (Phase 4)."""
    row = (
        await db.execute(select(WalkinBuyback).where(WalkinBuyback.id == buyback_id))
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Buyback not found")
    settings = await _load_settings(db)

    # Resolve a human description for the line and the cashier's name.
    description: str | None = None
    if row.coin_type_id:
        c = (await db.execute(select(CoinType).where(CoinType.id == row.coin_type_id))).scalar_one_or_none()
        description = c.name_en if c else None
    elif row.ounce_type_id:
        o = (await db.execute(select(OunceType).where(OunceType.id == row.ounce_type_id))).scalar_one_or_none()
        description = o.name_en if o else None
    elif row.product_id:
        p = (await db.execute(select(Product).where(Product.id == row.product_id))).scalar_one_or_none()
        description = p.name_en if p else None

    cashier = (await db.execute(select(User).where(User.id == row.cashier_id))).scalar_one_or_none()
    return build_buyback_receipt(
        row, settings,
        item_description=description,
        cashier_name=cashier.name if cashier else None,
    )
