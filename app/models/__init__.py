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
    K24 = "K24"


class OrderStatus(str, enum.Enum):
    COMPLETED = "COMPLETED"
    REFUNDED = "REFUNDED"
    VOIDED = "VOIDED"


class PaymentMethod(str, enum.Enum):
    CASH = "CASH"
    CARD = "CARD"
    MIXED = "MIXED"


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
    product_id: Mapped[str] = mapped_column(String, ForeignKey("products.id"), nullable=False)
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
    product: Mapped["Product"] = relationship(back_populates="order_items")


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
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
