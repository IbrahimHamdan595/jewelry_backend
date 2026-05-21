import enum
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    JSON, Boolean, DateTime, Enum, ForeignKey, Index, Integer, Numeric, String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base import Base


def _uid() -> str:
    return uuid4().hex


# ── Enums ─────────────────────────────────────────────────────────────────────

class Role(str, enum.Enum):
    ADMIN = "ADMIN"
    CASHIER = "CASHIER"


class Karat(str, enum.Enum):
    K18 = "K18"
    K21 = "K21"
    K22 = "K22"
    K24 = "K24"


class OrderStatus(str, enum.Enum):
    COMPLETED = "COMPLETED"
    REFUNDED = "REFUNDED"
    VOIDED = "VOIDED"


class PaymentMethod(str, enum.Enum):
    CASH = "CASH"
    CARD = "CARD"
    MIXED = "MIXED"


class LotSource(str, enum.Enum):
    BUYBACK = "BUYBACK"
    MELT = "MELT"
    SUPPLIER = "SUPPLIER"
    SEED = "SEED"
    ADJUSTMENT = "ADJUSTMENT"


class AdjustmentTarget(str, enum.Enum):
    LOT = "LOT"
    PRODUCT = "PRODUCT"
    COIN_STOCK = "COIN_STOCK"
    OUNCE_STOCK = "OUNCE_STOCK"


class AdjustmentReason(str, enum.Enum):
    LOSS = "LOSS"
    THEFT = "THEFT"
    GIFT = "GIFT"
    SAMPLE = "SAMPLE"
    CORRECTION = "CORRECTION"


class MarginMode(str, enum.Enum):
    USD = "USD"
    PERCENT = "PERCENT"


class BuybackKind(str, enum.Enum):
    PURE_GOLD = "PURE_GOLD"
    COIN = "COIN"
    OUNCE = "OUNCE"
    USED_PRODUCT = "USED_PRODUCT"


class BuybackMarginMode(str, enum.Enum):
    USD_PER_GRAM = "USD_PER_GRAM"
    PERCENT = "PERCENT"


class BuybackPriceMode(str, enum.Enum):
    FORMULA = "FORMULA"
    MANUAL = "MANUAL"


class ProductStatus(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    SOLD = "SOLD"
    MELTED = "MELTED"
    RESERVED = "RESERVED"
    INACTIVE = "INACTIVE"


class OrderItemKind(str, enum.Enum):
    PRODUCT = "PRODUCT"
    COIN = "COIN"
    OUNCE = "OUNCE"


class SupplierPurchaseMode(str, enum.Enum):
    CASH = "CASH"
    GOLD = "GOLD"
    MIXED = "MIXED"


class SupplierItemKind(str, enum.Enum):
    PRODUCT = "PRODUCT"
    COIN = "COIN"
    OUNCE = "OUNCE"
    PURE_GOLD = "PURE_GOLD"


class DebtUnit(str, enum.Enum):
    CASH = "CASH"
    GOLD = "GOLD"


# ── Models ────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[Role] = mapped_column(Enum(Role, name="role_enum"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    orders: Mapped[list["Order"]] = relationship(back_populates="cashier")


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    name_en: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name_ar: Mapped[str] = mapped_column(String, nullable=False, default="")
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    products: Mapped[list["Product"]] = relationship(back_populates="category_rel")


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name_en: Mapped[str] = mapped_column(String, nullable=False)
    name_ar: Mapped[str] = mapped_column(String, nullable=False, default="")
    category: Mapped[str] = mapped_column(String, nullable=False)
    category_id: Mapped[str | None] = mapped_column(String, ForeignKey("categories.id"), nullable=True)
    karat: Mapped[Karat] = mapped_column(Enum(Karat, name="karat_enum"), nullable=False)
    weight_grams: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    margin_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    making_charge: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    photos: Mapped[list] = mapped_column(JSON, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Phase 4
    is_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cost_basis_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    status: Mapped[ProductStatus] = mapped_column(
        Enum(ProductStatus, name="productstatus_enum"),
        nullable=False,
        default=ProductStatus.AVAILABLE,
    )
    source_ref_type: Mapped[str | None] = mapped_column(String, nullable=True)
    source_ref_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    category_rel: Mapped["Category | None"] = relationship(back_populates="products")
    order_items: Mapped[list["OrderItem"]] = relationship(back_populates="product")

    __table_args__ = (
        Index("ix_products_code", "code"),
        Index("ix_products_category_karat", "category", "karat"),
    )


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    order_number: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus, name="orderstatus_enum"), default=OrderStatus.COMPLETED)
    payment_method: Mapped[PaymentMethod] = mapped_column(Enum(PaymentMethod, name="paymentmethod_enum"), nullable=False)
    customer_name: Mapped[str | None] = mapped_column(String, nullable=True)
    cashier_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    vat_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    vat_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    total_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    total_lbp: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    lbp_exchange_rate: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    voided_by: Mapped[str | None] = mapped_column(String, nullable=True)
    void_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    cashier: Mapped["User"] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_orders_created_at", "created_at"),
        Index("ix_orders_cashier", "cashier_id"),
        Index("ix_orders_status", "status"),
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    order_id: Mapped[str] = mapped_column(String, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    # Phase 4: item_kind discriminator. product_id becomes nullable; coin/ounce FKs added.
    item_kind: Mapped[OrderItemKind] = mapped_column(
        Enum(OrderItemKind, name="orderitemkind_enum"),
        nullable=False,
        default=OrderItemKind.PRODUCT,
    )
    product_id: Mapped[str | None] = mapped_column(String, ForeignKey("products.id"), nullable=True)
    coin_type_id: Mapped[str | None] = mapped_column(String, ForeignKey("coin_types.id"), nullable=True)
    ounce_type_id: Mapped[str | None] = mapped_column(String, ForeignKey("ounce_types.id"), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Pricing snapshot — same columns reused for all three kinds.
    product_code: Mapped[str] = mapped_column(String, nullable=False)
    product_name: Mapped[str] = mapped_column(String, nullable=False)
    karat: Mapped[Karat] = mapped_column(Enum(Karat, name="karat_enum"), nullable=False)
    weight_grams: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    gold_rate_at_sale: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    margin_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    making_charge: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    final_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    order: Mapped["Order"] = relationship(back_populates="items")
    product: Mapped["Product | None"] = relationship(back_populates="order_items")


class GoldRateHistory(Base):
    __tablename__ = "gold_rate_history"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    rate_24k: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_gold_history_fetched_at", "fetched_at"),)


class GoldRateOverride(Base):
    __tablename__ = "gold_rate_overrides"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    rate_24k: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    set_by: Mapped[str] = mapped_column(String, nullable=False)
    set_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (Index("ix_gold_override_active", "is_active"),)


class Settings(Base):
    __tablename__ = "settings"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: "singleton")
    store_name: Mapped[str] = mapped_column(String, nullable=False, default="MAISON ZAHAB")
    store_name_ar: Mapped[str | None] = mapped_column(String, nullable=True)
    logo_url: Mapped[str | None] = mapped_column(String, nullable=True)
    address: Mapped[str] = mapped_column(String, nullable=False, default="")
    phone: Mapped[str] = mapped_column(String, nullable=False, default="")
    vat_number: Mapped[str | None] = mapped_column(String, nullable=True)
    default_margin_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=Decimal("15"))
    default_making_charge: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("25"))
    markup_k18: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0"))
    markup_k21: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0"))
    markup_k24: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0"))
    vat_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=Decimal("11"))
    lbp_exchange_rate: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("89500"))
    receipt_footer: Mapped[str | None] = mapped_column(String, nullable=True)
    gold_refresh_minutes: Mapped[int] = mapped_column(Integer, default=15)
    # Phase 3: buyback defaults
    default_buyback_margin_mode: Mapped[BuybackMarginMode] = mapped_column(
        Enum(BuybackMarginMode, name="buybackmarginmode_enum"),
        nullable=False,
        default=BuybackMarginMode.USD_PER_GRAM,
    )
    default_buyback_margin_value: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False, default=Decimal("2")
    )
    buyback_rate_drift_pct_max: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("2")
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ── Inventory layer (Phase 1) ─────────────────────────────────────────────────

class GoldLot(Base):
    __tablename__ = "gold_lots"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    karat: Mapped[Karat] = mapped_column(Enum(Karat, name="karat_enum"), nullable=False)
    weight_grams: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    weight_remaining_grams: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    source: Mapped[LotSource] = mapped_column(Enum(LotSource, name="lotsource_enum"), nullable=False)
    source_ref_type: Mapped[str | None] = mapped_column(String, nullable=True)
    source_ref_id: Mapped[str | None] = mapped_column(String, nullable=True)
    cost_basis_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    is_depleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    consumptions: Mapped[list["GoldLotConsumption"]] = relationship(back_populates="lot")

    __table_args__ = (
        Index("ix_gold_lots_karat", "karat"),
        Index("ix_gold_lots_is_depleted", "is_depleted"),
    )


class GoldLotConsumption(Base):
    __tablename__ = "gold_lot_consumptions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    lot_id: Mapped[str] = mapped_column(String, ForeignKey("gold_lots.id"), nullable=False)
    grams: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    cost_basis_consumed_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    ref_type: Mapped[str] = mapped_column(String, nullable=False)
    ref_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)

    lot: Mapped["GoldLot"] = relationship(back_populates="consumptions")

    __table_args__ = (
        Index("ix_lot_consumptions_lot", "lot_id"),
        Index("ix_lot_consumptions_ref", "ref_type", "ref_id"),
    )


class InventoryLedger(Base):
    __tablename__ = "inventory_ledger"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    actor_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ref_type: Mapped[str] = mapped_column(String, nullable=False)
    ref_id: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_inventory_ledger_event_type", "event_type"),
        Index("ix_inventory_ledger_ref", "ref_type", "ref_id"),
        Index("ix_inventory_ledger_occurred_at", "occurred_at"),
    )


class ManualAdjustment(Base):
    __tablename__ = "manual_adjustments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    target_type: Mapped[AdjustmentTarget] = mapped_column(Enum(AdjustmentTarget, name="adjustmenttarget_enum"), nullable=False)
    target_id: Mapped[str] = mapped_column(String, nullable=False)
    delta: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    reason: Mapped[AdjustmentReason] = mapped_column(Enum(AdjustmentReason, name="adjustmentreason_enum"), nullable=False)
    notes: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_manual_adjustments_target", "target_type", "target_id"),
    )


# ── Inventory layer (Phase 2: coin & ounce catalogs) ──────────────────────────

class CoinType(Base):
    __tablename__ = "coin_types"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name_en: Mapped[str] = mapped_column(String, nullable=False)
    name_ar: Mapped[str] = mapped_column(String, nullable=False, default="")
    karat: Mapped[Karat] = mapped_column(Enum(Karat, name="karat_enum"), nullable=False)
    weight_grams: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    markup_per_gram: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, default=Decimal("0"))
    margin_mode: Mapped[MarginMode] = mapped_column(Enum(MarginMode, name="marginmode_enum"), nullable=False)
    margin_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    on_hand_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    min_stock_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    photo_url: Mapped[str | None] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_coin_types_code", "code"),
        Index("ix_coin_types_is_active", "is_active"),
    )


class OunceType(Base):
    __tablename__ = "ounce_types"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name_en: Mapped[str] = mapped_column(String, nullable=False)
    name_ar: Mapped[str] = mapped_column(String, nullable=False, default="")
    karat: Mapped[Karat] = mapped_column(Enum(Karat, name="karat_enum"), nullable=False)
    weight_grams: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    markup_per_gram: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, default=Decimal("0"))
    margin_mode: Mapped[MarginMode] = mapped_column(Enum(MarginMode, name="marginmode_enum"), nullable=False)
    margin_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    on_hand_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    min_stock_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    photo_url: Mapped[str | None] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_ounce_types_code", "code"),
        Index("ix_ounce_types_is_active", "is_active"),
    )


# ── Inventory layer (Phase 3: walk-in buybacks) ───────────────────────────────

class WalkinBuyback(Base):
    __tablename__ = "walkin_buybacks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    seller_name: Mapped[str] = mapped_column(String, nullable=False)
    seller_phone: Mapped[str] = mapped_column(String, nullable=False)
    cashier_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    kind: Mapped[BuybackKind] = mapped_column(Enum(BuybackKind, name="buybackkind_enum"), nullable=False)
    # Result references — exactly one populated per `kind`.
    result_lot_id: Mapped[str | None] = mapped_column(String, ForeignKey("gold_lots.id"), nullable=True)
    coin_type_id: Mapped[str | None] = mapped_column(String, ForeignKey("coin_types.id"), nullable=True)
    ounce_type_id: Mapped[str | None] = mapped_column(String, ForeignKey("ounce_types.id"), nullable=True)
    product_id: Mapped[str | None] = mapped_column(String, ForeignKey("products.id"), nullable=True)
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    weight_grams: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    karat: Mapped[Karat | None] = mapped_column(Enum(Karat, name="karat_enum"), nullable=True)
    buy_price_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    gold_rate_at_buy: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    buyback_margin_mode: Mapped[BuybackMarginMode | None] = mapped_column(
        Enum(BuybackMarginMode, name="buybackmarginmode_enum"), nullable=True
    )
    buyback_margin_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    price_mode: Mapped[BuybackPriceMode] = mapped_column(
        Enum(BuybackPriceMode, name="buybackpricemode_enum"), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_walkin_buybacks_occurred_at", "occurred_at"),
        Index("ix_walkin_buybacks_kind", "kind"),
        Index("ix_walkin_buybacks_cashier", "cashier_id"),
    )


# ── Inventory layer (Phase 5: suppliers + procurement + AP) ───────────────────

class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    contact_name: Mapped[str | None] = mapped_column(String, nullable=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    address: Mapped[str | None] = mapped_column(String, nullable=True)
    default_currency: Mapped[str] = mapped_column(String, nullable=False, default="USD")
    payment_terms: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (Index("ix_suppliers_is_active", "is_active"),)


class SupplierPurchase(Base):
    __tablename__ = "supplier_purchases"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    supplier_id: Mapped[str] = mapped_column(String, ForeignKey("suppliers.id"), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    payment_mode: Mapped[SupplierPurchaseMode] = mapped_column(
        Enum(SupplierPurchaseMode, name="supplierpurchasemode_enum"),
        nullable=False,
    )
    trade_markup_per_gram: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    total_cash_due: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    # JSONB-ish dicts keyed by karat enum value: {"K21": "50.000", ...}
    total_grams_due_by_karat: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    cash_paid_at_creation: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    grams_paid_at_creation_by_karat: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    items: Mapped[list["SupplierPurchaseItem"]] = relationship(
        back_populates="purchase", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_supplier_purchases_supplier", "supplier_id"),
        Index("ix_supplier_purchases_occurred_at", "occurred_at"),
    )


class SupplierPurchaseItem(Base):
    __tablename__ = "supplier_purchase_items"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    purchase_id: Mapped[str] = mapped_column(String, ForeignKey("supplier_purchases.id", ondelete="CASCADE"), nullable=False)
    item_kind: Mapped[SupplierItemKind] = mapped_column(
        Enum(SupplierItemKind, name="supplieritemkind_enum"), nullable=False,
    )
    product_id: Mapped[str | None] = mapped_column(String, ForeignKey("products.id"), nullable=True)
    coin_type_id: Mapped[str | None] = mapped_column(String, ForeignKey("coin_types.id"), nullable=True)
    ounce_type_id: Mapped[str | None] = mapped_column(String, ForeignKey("ounce_types.id"), nullable=True)
    lot_id: Mapped[str | None] = mapped_column(String, ForeignKey("gold_lots.id"), nullable=True)
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    weight_grams: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    karat: Mapped[Karat | None] = mapped_column(Enum(Karat, name="karat_enum"), nullable=True)
    unit_cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    notes: Mapped[str | None] = mapped_column(String, nullable=True)

    purchase: Mapped["SupplierPurchase"] = relationship(back_populates="items")


class SupplierPayment(Base):
    __tablename__ = "supplier_payments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    supplier_id: Mapped[str] = mapped_column(String, ForeignKey("suppliers.id"), nullable=False)
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    unit: Mapped[DebtUnit] = mapped_column(Enum(DebtUnit, name="debtunit_enum"), nullable=False)
    karat: Mapped[Karat | None] = mapped_column(Enum(Karat, name="karat_enum"), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    source_lot_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    paid_by_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("ix_supplier_payments_supplier", "supplier_id"),
        Index("ix_supplier_payments_paid_at", "paid_at"),
    )


class SupplierBalance(Base):
    __tablename__ = "supplier_balances"

    # Composite PK. karat is empty string "" for CASH rows (NULL in composite
    # PKs is awkward in PG since NULL != NULL).
    supplier_id: Mapped[str] = mapped_column(String, ForeignKey("suppliers.id"), primary_key=True)
    unit: Mapped[DebtUnit] = mapped_column(Enum(DebtUnit, name="debtunit_enum"), primary_key=True)
    karat: Mapped[str] = mapped_column(String, primary_key=True, default="")
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False, default=Decimal("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
