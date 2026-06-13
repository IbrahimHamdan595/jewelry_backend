"""Receipt builders (Phase 0).

Pure functions that turn a persisted transaction (`Order`, `SupplierPurchase`,
`WalkinBuyback`) plus the store `Settings` into the normalized `ReceiptOut`
shape. No DB access and no clock — callers load the rows (with the relationships
they need) and pass them in, which keeps these trivially unit-testable.

The three builders are the single place that knows how each source maps onto the
shared receipt; the API layer (Phase 4) just loads rows and calls the matching
builder.
"""
from __future__ import annotations

from decimal import Decimal

from app.core.pricing import KARAT_LABEL
from app.models import (
    Karat,
    Order,
    Settings,
    SupplierPurchase,
    WalkinBuyback,
)
from app.schemas.receipt import (
    ReceiptLine,
    ReceiptOut,
    ReceiptParty,
    ReceiptStore,
    ReceiptTotals,
    ReceiptType,
)


def _karat_label(karat) -> str | None:
    if karat is None:
        return None
    try:
        return KARAT_LABEL[karat if isinstance(karat, Karat) else Karat(karat)]
    except (KeyError, ValueError):
        return str(getattr(karat, "value", karat))


def _store_header(settings: Settings) -> ReceiptStore:
    return ReceiptStore(
        name=settings.store_name,
        name_ar=settings.store_name_ar,
        logo_url=settings.logo_url,
        address=settings.address or "",
        phone=settings.phone or "",
        vat_number=settings.vat_number,
        footer=settings.receipt_footer,
    )


def build_sale_receipt(order: Order, settings: Settings) -> ReceiptOut:
    """Customer sale. `order.items` and `order.cashier` must be loaded."""
    lines = [
        ReceiptLine(
            description=item.product_name,
            code=item.product_code,
            karat=_karat_label(item.karat),
            weight_grams=item.weight_grams,
            quantity=Decimal(item.quantity),
            unit_price=(
                (item.final_price / item.quantity) if item.quantity else item.final_price
            ),
            stone_value=getattr(item, "stone_value_at_sale", None),
            line_total=item.final_price,
        )
        for item in order.items
    ]

    # Discount fields are forward-looking (Phase 2). getattr keeps this builder
    # working against Order rows that predate the discount columns.
    discount_percent = getattr(order, "discount_percent", None)
    discount_amount = getattr(order, "discount_amount", None)

    totals = ReceiptTotals(
        subtotal=order.subtotal,
        discount_percent=discount_percent or None,
        discount_amount=discount_amount or None,
        vat_percent=order.vat_percent,
        vat_amount=order.vat_amount,
        total_usd=order.total_usd,
        total_lbp=order.total_lbp,
        lbp_exchange_rate=order.lbp_exchange_rate,
    )

    return ReceiptOut(
        type=ReceiptType.SALE,
        reference=order.order_number,
        issued_at=order.created_at,
        store=_store_header(settings),
        cashier_name=order.cashier.name if order.cashier else None,
        party=ReceiptParty(role="customer", name=order.customer_name),
        lines=lines,
        totals=totals,
        payment_method=order.payment_method.value,
    )


def build_supplier_receipt(
    purchase: SupplierPurchase,
    settings: Settings,
    *,
    supplier_name: str | None = None,
    cashier_name: str | None = None,
    item_descriptions: dict[str, str] | None = None,
) -> ReceiptOut:
    """Supplier purchase. `purchase.items` must be loaded.

    `supplier_name` and per-item human descriptions are resolved by the caller
    (the purchase items reference products/coins/ounces by id); when omitted we
    fall back to the item kind + karat so the builder never fails.
    """
    item_descriptions = item_descriptions or {}
    lines: list[ReceiptLine] = []
    subtotal = Decimal("0")
    for item in purchase.items:
        qty = Decimal(item.quantity) if item.quantity is not None else Decimal("1")
        line_total = (item.unit_cost_usd or Decimal("0")) * qty
        subtotal += line_total
        desc = item_descriptions.get(item.id) or item.item_kind.value.replace("_", " ").title()
        lines.append(
            ReceiptLine(
                description=desc,
                karat=_karat_label(item.karat),
                weight_grams=item.weight_grams,
                quantity=qty,
                unit_price=item.unit_cost_usd,
                line_total=line_total,
            )
        )

    totals = ReceiptTotals(
        subtotal=subtotal,
        total_usd=subtotal,
    )

    return ReceiptOut(
        type=ReceiptType.SUPPLIER_PURCHASE,
        reference=purchase.id,
        issued_at=purchase.occurred_at,
        store=_store_header(settings),
        cashier_name=cashier_name,
        party=ReceiptParty(role="supplier", name=supplier_name),
        lines=lines,
        totals=totals,
        payment_method=purchase.payment_mode.value,
        notes=purchase.notes,
    )


def build_buyback_receipt(
    buyback: WalkinBuyback,
    settings: Settings,
    *,
    item_description: str | None = None,
    cashier_name: str | None = None,
) -> ReceiptOut:
    """Walk-in buyback. The shop pays the seller `buy_price_usd`."""
    qty = Decimal(buyback.quantity) if buyback.quantity is not None else Decimal("1")
    desc = item_description or buyback.kind.value.replace("_", " ").title()
    line = ReceiptLine(
        description=desc,
        karat=_karat_label(buyback.karat),
        weight_grams=buyback.weight_grams,
        quantity=qty,
        unit_price=(buyback.buy_price_usd / qty) if qty else buyback.buy_price_usd,
        line_total=buyback.buy_price_usd,
    )

    totals = ReceiptTotals(
        subtotal=buyback.buy_price_usd,
        total_usd=buyback.buy_price_usd,
    )

    return ReceiptOut(
        type=ReceiptType.BUYBACK,
        reference=buyback.id,
        issued_at=buyback.occurred_at,
        store=_store_header(settings),
        cashier_name=cashier_name,
        party=ReceiptParty(
            role="seller", name=buyback.seller_name, phone=buyback.seller_phone
        ),
        lines=[line],
        totals=totals,
        notes=buyback.notes,
    )
