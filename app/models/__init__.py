import enum
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, Enum, ForeignKey, Index, Integer, Numeric, String,
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
    # Accounting section (design §3.5). Only ACCOUNTANT is wired now; MANAGER
    # is reserved for a future approvals/oversight role.
    ACCOUNTANT = "ACCOUNTANT"
    MANAGER = "MANAGER"


class Karat(str, enum.Enum):
    K18 = "K18"
    K21 = "K21"
    K22 = "K22"
    K24 = "K24"


# ── Accounting (GL Core, Module 0) ────────────────────────────────────────────

class AccountType(str, enum.Enum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"


class Denomination(str, enum.Enum):
    """What a GL account can carry on its lines (design §3.2)."""
    MONEY = "MONEY"   # cash, bank, revenue, VAT, expenses, AR control
    METAL = "METAL"   # pure-gram memo/position accounts
    DUAL = "DUAL"     # metal inventory / metal AP / metal COGS — money AND grams


class NormalBalance(str, enum.Enum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class PeriodStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class OrderStatus(str, enum.Enum):
    COMPLETED = "COMPLETED"
    REFUNDED = "REFUNDED"
    # Phase 1 (per-item refunds): at least one line refunded but not all.
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
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


# Audit phase B2 — physical stock-take workflow.
class StockTakeStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    CLOSED = "CLOSED"


class StockTakeLineResolution(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    NO_VARIANCE = "NO_VARIANCE"


class StockTakeRefType(str, enum.Enum):
    """What a stock-take line counts against. Mirrors a subset of
    AdjustmentTarget by VALUE — but the mapping between the two is
    explicit (`app/core/stock_take.py::to_adjustment_target`) and tested.
    DO NOT assume name parity guarantees correctness."""
    COIN_STOCK = "COIN_STOCK"
    OUNCE_STOCK = "OUNCE_STOCK"


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
    # Phase 3 (product quantity): products are stocked-by-quantity, not 1-of-1.
    # `status` is kept as a DERIVED compatibility flag: sale/refund/void flows set
    # it to SOLD when on_hand_qty hits 0 and AVAILABLE when > 0, but they never
    # overwrite the explicit MELTED / INACTIVE states. min_stock_qty drives
    # low-stock alerts (NULL = no alert).
    on_hand_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    min_stock_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    # Phase 2 — order-level discount. VAT is charged on the PRE-discount subtotal;
    # the discount is then subtracted from the grand total. discount_amount is the
    # resolved USD value of discount_percent applied to subtotal.
    discount_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=Decimal("0"))
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
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
    # Phase 1 (per-item refunds). refunded_qty counts units returned to stock
    # (0 or 1 for atomic PRODUCT lines; 0..quantity for COIN/OUNCE lines).
    # refunded_amount is the cumulative pre-VAT value refunded for this line.
    refunded_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    refunded_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    order: Mapped["Order"] = relationship(back_populates="items")
    product: Mapped["Product | None"] = relationship(back_populates="order_items")


class GoldRateHistory(Base):
    __tablename__ = "gold_rate_history"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    rate_24k: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    # Phase 6 (#7): store each karat's value at poll time (exact). Nullable for
    # pre-Phase-6 rows; those are backfilled via purity multipliers and flagged
    # with per_karat_backfilled=True (derived, not actually polled per-karat).
    rate_22k: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    rate_21k: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    rate_18k: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    per_karat_backfilled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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
    store_name: Mapped[str] = mapped_column(String, nullable=False, default="Fawaz El Namel")
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
    # Phase 2 — max order-level discount % a cashier may apply without override.
    # Defaults to 0 (discounts disabled until an admin raises the cap).
    max_discount_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=Decimal("0"))
    receipt_footer: Mapped[str | None] = mapped_column(String, nullable=True)
    gold_refresh_minutes: Mapped[int] = mapped_column(Integer, default=15)
    # Zakat
    nisab_grams: Mapped[Decimal] = mapped_column(
        Numeric(10, 3), nullable=False, default=Decimal("85.000")
    )
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
    # Accounting (Module 1) — master switch for real-time auto-posting to the GL.
    # Default OFF so operations behave exactly as before until accounting is set
    # up (CoA seeded + opening balances). Flip ON to post every operation.
    accounting_auto_post_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
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
    # Hash chain (audit phase A1). prev_hash is the previous row's entry_hash,
    # or GENESIS_HASH for the first row. entry_hash is sha256 over
    # (canonical(payload+meta) || prev_hash). NOT NULL + UNIQUE on entry_hash
    # enforced at the DB layer after the A1.2 backfill.
    prev_hash: Mapped[str] = mapped_column(String, nullable=False)
    entry_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_inventory_ledger_event_type", "event_type"),
        Index("ix_inventory_ledger_ref", "ref_type", "ref_id"),
        Index("ix_inventory_ledger_occurred_at", "occurred_at"),
    )


class AuthAuditLog(Base):
    """Authentication & user-management audit log (audit phase A3b).

    Kept separate from `InventoryLedger` because:
      • Writes can be unauthenticated (failed logins) and high-volume
        (bot probes). Mixing them with inventory events would noise up
        every supplier-debt reconciliation query.
      • Writes are *best effort* (see `app/core/auth_audit.py`): a logging
        failure must NEVER block a legitimate login. The inventory ledger
        is the opposite — its writes are inside the caller's transaction
        and a failure rolls everything back.
      • Different retention rule (default 18 months, configurable).

    `user_id` is intentionally NOT a foreign key: failed-login attempts
    may carry a `claimed_email` that doesn't correspond to any real user
    (or that is a deliberate attack probe). The column captures what was
    submitted to /login, not a verified identity.
    """
    __tablename__ = "auth_audit_log"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # NO FK to users — the claimed email may be garbage. user_id is populated
    # only when an authenticated event ties to an existing user row.
    user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    claimed_email: Mapped[str | None] = mapped_column(String, nullable=True)
    client_ip: Mapped[str | None] = mapped_column(String, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[str | None] = mapped_column(String, nullable=True)
    # When this row is eligible for deletion by the future pruner. Storing
    # the absolute timestamp (not a duration) makes the deletion query a
    # simple `WHERE retention_until_at < now()` and is robust to later
    # changes in the configured retention window.
    retention_until_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Hash chain — same recipe as inventory ledger but a separate chain.
    prev_hash: Mapped[str] = mapped_column(String, nullable=False)
    entry_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_auth_audit_log_event_type", "event_type"),
        Index("ix_auth_audit_log_user_id", "user_id"),
        Index("ix_auth_audit_log_claimed_email", "claimed_email"),
        Index("ix_auth_audit_log_occurred_at", "occurred_at"),
        Index("ix_auth_audit_log_retention_until_at", "retention_until_at"),
    )


class AuthAuditChainHead(Base):
    """Single-row table tracking the auth-audit chain head — sibling of
    `InventoryLedgerChainHead`. Independent lock so a burst of failed-login
    probes can't contend with inventory writes."""
    __tablename__ = "auth_audit_chain_head"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    latest_entry_hash: Mapped[str] = mapped_column(String, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class InventoryLedgerChainHead(Base):
    """Single-row table tracking the latest entry_hash of the ledger chain.

    `record()` SELECT ... FOR UPDATE locks this row before computing the next
    entry_hash, which serializes concurrent ledger appends without needing
    Postgres-specific advisory locks (and works under the SQLite test fixture).
    """
    __tablename__ = "inventory_ledger_chain_head"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    latest_entry_hash: Mapped[str] = mapped_column(String, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
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


# ── Zakat ─────────────────────────────────────────────────────────────────────

class ZakatSnapshot(Base):
    __tablename__ = "zakat_snapshots"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    assessment_date: Mapped[date] = mapped_column(Date, nullable=False)
    taken_by_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)

    # Snapshotted inputs
    gold_rate_24k_usd_per_gram: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    gold_rate_source: Mapped[str] = mapped_column(String, nullable=False)
    nisab_grams_used: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)

    # Computed outputs
    total_au_grams: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    total_au_value_usd: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    zakat_au_grams: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    zakat_value_usd: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    meets_nisab: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # Per-karat structured breakdown (audit trail).
    # Shape: {"K18": {"products":"...","coins":"...","ounces":"...","lots":"...","total_grams":"...","au_grams":"..."}, ...}
    breakdown_by_karat: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # sha256 over canonical JSON of inputs+outputs; recomputed on read for tamper detection.
    integrity_hash: Mapped[str] = mapped_column(String, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_zakat_snapshots_assessment_date", "assessment_date"),
        Index("ix_zakat_snapshots_taken_at", "taken_at"),
    )


# ── Stock-take workflow (audit phase B2) ─────────────────────────────────────

class StockTake(Base):
    """A physical inventory count session.

    AUDIT: stock_takes is a WORKFLOW table — not append-only. Status legitimately
    moves DRAFT → SUBMITTED → CLOSED. The audit guarantee for inventory
    mutations lives in the chained ManualAdjustment / ledger events that
    APPROVED lines emit through `apply_manual_adjustment_core`. The stock-take
    row itself is the proposer, not the writer.
    """
    __tablename__ = "stock_takes"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_by_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), nullable=False
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[StockTakeStatus] = mapped_column(
        Enum(StockTakeStatus, name="stocktakestatus_enum"),
        nullable=False,
        default=StockTakeStatus.DRAFT,
    )
    notes: Mapped[str | None] = mapped_column(String, nullable=True)

    lines: Mapped[list["StockTakeLine"]] = relationship(
        back_populates="stock_take", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_stock_takes_status", "status"),
        Index("ix_stock_takes_started_at", "started_at"),
    )


class StockTakeLine(Base):
    """One counted line in a stock-take session.

    AUDIT: on APPROVED resolution, `adjustment_id` is the FK to the
    ManualAdjustment row that posted the variance. That row is the audit
    source-of-truth for how `on_hand_qty` actually changed; this row is
    the workflow record that explains why.
    """
    __tablename__ = "stock_take_lines"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    stock_take_id: Mapped[str] = mapped_column(
        String, ForeignKey("stock_takes.id", ondelete="CASCADE"), nullable=False
    )
    ref_type: Mapped[StockTakeRefType] = mapped_column(
        Enum(StockTakeRefType, name="stocktakereftype_enum"), nullable=False
    )
    ref_id: Mapped[str] = mapped_column(String, nullable=False)
    counted_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    # Snapshot taken at submit time so variance is stable even if a
    # concurrent sale changes `on_hand_qty` afterwards.
    expected_qty_at_submit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    variance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolution: Mapped[StockTakeLineResolution] = mapped_column(
        Enum(StockTakeLineResolution, name="stocktakelineresolution_enum"),
        nullable=False,
        default=StockTakeLineResolution.PENDING,
    )
    rejection_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    # FK to the ManualAdjustment posted on approval. NULL otherwise.
    adjustment_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("manual_adjustments.id"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by_user_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    stock_take: Mapped["StockTake"] = relationship(back_populates="lines")

    __table_args__ = (
        Index("ix_stock_take_lines_parent", "stock_take_id"),
        Index("ix_stock_take_lines_resolution", "resolution"),
        # A given coin/ounce type can only appear once per stock-take —
        # the operator counts each type exactly once.
        Index(
            "uq_stock_take_lines_unique_per_take",
            "stock_take_id", "ref_type", "ref_id",
            unique=True,
        ),
    )


# ── Accounting (GL Core, Module 0) ────────────────────────────────────────────

# Journal entry source types are documentation-style STRINGS (like
# InventoryLedger.event_type) so new operations add a source without a schema
# migration. Constants live in app/core/gl.py.

class GLAccount(Base):
    """Chart-of-accounts node. Metal/DUAL accounts are karat-agnostic
    containers; per-karat balance is enforced by the posting engine and
    reported as a breakdown (design §3.3)."""
    __tablename__ = "gl_accounts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[AccountType] = mapped_column(Enum(AccountType, name="gl_account_type_enum"), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(String, ForeignKey("gl_accounts.id"), nullable=True)
    denomination: Mapped[Denomination] = mapped_column(
        Enum(Denomination, name="gl_denomination_enum"), nullable=False, default=Denomination.MONEY
    )
    currency: Mapped[str | None] = mapped_column(String, nullable=True)  # money accounts; NULL for pure METAL
    # Reserved (design §3.3): karat lives on the line in M0, NULL on the account.
    karat: Mapped[str | None] = mapped_column(String, nullable=True)
    system_key: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    normal_balance: Mapped[NormalBalance] = mapped_column(
        Enum(NormalBalance, name="gl_normal_balance_enum"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_gl_accounts_type", "type"),
        Index("ix_gl_accounts_parent", "parent_id"),
    )


class GLPeriod(Base):
    """Monthly accounting period. period_no is the month 1..12 (design §16: monthly)."""
    __tablename__ = "gl_periods"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    period_no: Mapped[int] = mapped_column(Integer, nullable=False)  # 1..12
    status: Mapped[PeriodStatus] = mapped_column(
        Enum(PeriodStatus, name="gl_period_status_enum"), nullable=False, default=PeriodStatus.OPEN
    )
    closed_by_user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("uq_gl_periods_year_period", "year", "period_no", unique=True),
    )


class GLJournalEntry(Base):
    """Append-only, hash-chained journal-entry header (design §3.3)."""
    __tablename__ = "gl_journal_entries"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    entry_no: Mapped[str] = mapped_column(String, unique=True, nullable=False)  # JE-YYYYMMDD-NNN
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    period_id: Mapped[str] = mapped_column(String, ForeignKey("gl_periods.id"), nullable=False)
    memo: Mapped[str] = mapped_column(String, nullable=False, default="")
    source_type: Mapped[str] = mapped_column(String, nullable=False)   # MANUAL/ORDER/... (string)
    source_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reverses_entry_id: Mapped[str | None] = mapped_column(String, ForeignKey("gl_journal_entries.id"), nullable=True)
    actor_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    prev_hash: Mapped[str] = mapped_column(String, nullable=False)
    entry_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    lines: Mapped[list["GLJournalLine"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan", order_by="GLJournalLine.line_no"
    )

    __table_args__ = (
        Index("ix_gl_entries_entry_date", "entry_date"),
        Index("ix_gl_entries_period", "period_id"),
        Index("ix_gl_entries_source", "source_type", "source_id"),
    )


class GLJournalLine(Base):
    """One debit/credit line. A line populates the money fields, the metal
    fields, or both (DUAL accounts) — design §3.2/§3.3."""
    __tablename__ = "gl_journal_lines"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    entry_id: Mapped[str] = mapped_column(String, ForeignKey("gl_journal_entries.id", ondelete="CASCADE"), nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    account_id: Mapped[str] = mapped_column(String, ForeignKey("gl_accounts.id"), nullable=False)
    # Money component (txn currency + FX + USD base). USD lines: money == base, fx_rate = 1.
    money_debit: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    money_credit: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String, nullable=False, default="USD")
    fx_rate: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("1"))
    base_debit: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    base_credit: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    # Metal component (grams per karat).
    metal_debit_grams: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False, default=Decimal("0"))
    metal_credit_grams: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False, default=Decimal("0"))
    karat: Mapped[str | None] = mapped_column(String, nullable=True)
    memo: Mapped[str] = mapped_column(String, nullable=False, default="")

    entry: Mapped["GLJournalEntry"] = relationship(back_populates="lines")

    __table_args__ = (
        Index("ix_gl_lines_entry", "entry_id"),
        Index("ix_gl_lines_account", "account_id"),
    )


class GLJournalChainHead(Base):
    """Single-row table; locked FOR UPDATE during posting to serialize the GL
    hash chain — sibling of InventoryLedgerChainHead."""
    __tablename__ = "gl_journal_chain_head"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    latest_entry_hash: Mapped[str] = mapped_column(String, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class GLEntrySequence(Base):
    """Per-day entry-number counter (JE-YYYYMMDD-NNN). Locked FOR UPDATE so
    numbering is gap-tracked and never count()+1 (design §3.3)."""
    __tablename__ = "gl_entry_sequence"

    day_key: Mapped[str] = mapped_column(String, primary_key=True)  # "YYYYMMDD"
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
