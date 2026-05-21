"""inventory phase 5: suppliers, purchases, repayments, balances

Revision ID: a1b2c3d4e5f7
Revises: f0a1b2c3d4e5
Create Date: 2026-05-20 23:30:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a1b2c3d4e5f7"
down_revision = "f0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    purchase_mode = sa.Enum("CASH", "GOLD", "MIXED", name="supplierpurchasemode_enum")
    item_kind = sa.Enum("PRODUCT", "COIN", "OUNCE", "PURE_GOLD", name="supplieritemkind_enum")
    debt_unit = sa.Enum("CASH", "GOLD", name="debtunit_enum")
    purchase_mode.create(op.get_bind(), checkfirst=True)
    item_kind.create(op.get_bind(), checkfirst=True)
    debt_unit.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "suppliers",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("contact_name", sa.String(), nullable=True),
        sa.Column("phone", sa.String(), nullable=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("address", sa.String(), nullable=True),
        sa.Column("default_currency", sa.String(), nullable=False, server_default="USD"),
        sa.Column("payment_terms", sa.String(), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_suppliers_is_active", "suppliers", ["is_active"])

    op.create_table(
        "supplier_purchases",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("supplier_id", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "payment_mode",
            postgresql.ENUM(name="supplierpurchasemode_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("trade_markup_per_gram", sa.Numeric(10, 4), nullable=True),
        sa.Column("total_cash_due", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("total_grams_due_by_karat", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("cash_paid_at_creation", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("grams_paid_at_creation_by_karat", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("created_by_user_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
    )
    op.create_index("ix_supplier_purchases_supplier", "supplier_purchases", ["supplier_id"])
    op.create_index("ix_supplier_purchases_occurred_at", "supplier_purchases", ["occurred_at"])

    op.create_table(
        "supplier_purchase_items",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("purchase_id", sa.String(), nullable=False),
        sa.Column(
            "item_kind",
            postgresql.ENUM(name="supplieritemkind_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("product_id", sa.String(), nullable=True),
        sa.Column("coin_type_id", sa.String(), nullable=True),
        sa.Column("ounce_type_id", sa.String(), nullable=True),
        sa.Column("lot_id", sa.String(), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=True),
        sa.Column("weight_grams", sa.Numeric(10, 3), nullable=True),
        sa.Column("karat", postgresql.ENUM(name="karat_enum", create_type=False), nullable=True),
        sa.Column("unit_cost_usd", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("notes", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["purchase_id"], ["supplier_purchases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["coin_type_id"], ["coin_types.id"]),
        sa.ForeignKeyConstraint(["ounce_type_id"], ["ounce_types.id"]),
        sa.ForeignKeyConstraint(["lot_id"], ["gold_lots.id"]),
    )

    op.create_table(
        "supplier_payments",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("supplier_id", sa.String(), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "unit",
            postgresql.ENUM(name="debtunit_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("karat", postgresql.ENUM(name="karat_enum", create_type=False), nullable=True),
        sa.Column("amount", sa.Numeric(12, 3), nullable=False),
        sa.Column("source_lot_ids", sa.JSON(), nullable=True),
        sa.Column("paid_by_user_id", sa.String(), nullable=False),
        sa.Column("notes", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"]),
        sa.ForeignKeyConstraint(["paid_by_user_id"], ["users.id"]),
    )
    op.create_index("ix_supplier_payments_supplier", "supplier_payments", ["supplier_id"])
    op.create_index("ix_supplier_payments_paid_at", "supplier_payments", ["paid_at"])

    op.create_table(
        "supplier_balances",
        sa.Column("supplier_id", sa.String(), nullable=False),
        sa.Column(
            "unit",
            postgresql.ENUM(name="debtunit_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("karat", sa.String(), nullable=False, server_default=""),
        sa.Column("balance", sa.Numeric(12, 3), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("supplier_id", "unit", "karat"),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"]),
    )


def downgrade() -> None:
    op.drop_table("supplier_balances")
    op.drop_index("ix_supplier_payments_paid_at", "supplier_payments")
    op.drop_index("ix_supplier_payments_supplier", "supplier_payments")
    op.drop_table("supplier_payments")
    op.drop_table("supplier_purchase_items")
    op.drop_index("ix_supplier_purchases_occurred_at", "supplier_purchases")
    op.drop_index("ix_supplier_purchases_supplier", "supplier_purchases")
    op.drop_table("supplier_purchases")
    op.drop_index("ix_suppliers_is_active", "suppliers")
    op.drop_table("suppliers")

    sa.Enum(name="debtunit_enum").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="supplieritemkind_enum").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="supplierpurchasemode_enum").drop(op.get_bind(), checkfirst=True)
