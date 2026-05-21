"""inventory phase 4: product extensions + order_item kind discriminator

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-05-20 22:55:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f0a1b2c3d4e5"
down_revision = "e9f0a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # New enums
    product_status = sa.Enum(
        "AVAILABLE", "SOLD", "MELTED", "RESERVED", "INACTIVE",
        name="productstatus_enum",
    )
    order_item_kind = sa.Enum("PRODUCT", "COIN", "OUNCE", name="orderitemkind_enum")
    product_status.create(op.get_bind(), checkfirst=True)
    order_item_kind.create(op.get_bind(), checkfirst=True)

    # products: is_used, cost_basis_usd, status, source_ref_*
    op.add_column(
        "products",
        sa.Column("is_used", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "products",
        sa.Column("cost_basis_usd", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column(
        "products",
        sa.Column(
            "status",
            postgresql.ENUM(name="productstatus_enum", create_type=False),
            nullable=False,
            server_default="AVAILABLE",
        ),
    )
    op.add_column("products", sa.Column("source_ref_type", sa.String(), nullable=True))
    op.add_column("products", sa.Column("source_ref_id", sa.String(), nullable=True))

    # Backfill: products with is_active=false → status=INACTIVE
    op.execute("UPDATE products SET status = 'INACTIVE' WHERE is_active = false")

    # order_items: item_kind, coin_type_id, ounce_type_id, quantity; product_id nullable
    op.add_column(
        "order_items",
        sa.Column(
            "item_kind",
            postgresql.ENUM(name="orderitemkind_enum", create_type=False),
            nullable=False,
            server_default="PRODUCT",
        ),
    )
    op.add_column(
        "order_items",
        sa.Column("coin_type_id", sa.String(), nullable=True),
    )
    op.add_column(
        "order_items",
        sa.Column("ounce_type_id", sa.String(), nullable=True),
    )
    op.add_column(
        "order_items",
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_foreign_key(
        "fk_order_items_coin_type_id", "order_items", "coin_types",
        ["coin_type_id"], ["id"],
    )
    op.create_foreign_key(
        "fk_order_items_ounce_type_id", "order_items", "ounce_types",
        ["ounce_type_id"], ["id"],
    )
    op.alter_column("order_items", "product_id", nullable=True)


def downgrade() -> None:
    op.alter_column("order_items", "product_id", nullable=False)
    op.drop_constraint("fk_order_items_ounce_type_id", "order_items", type_="foreignkey")
    op.drop_constraint("fk_order_items_coin_type_id", "order_items", type_="foreignkey")
    op.drop_column("order_items", "quantity")
    op.drop_column("order_items", "ounce_type_id")
    op.drop_column("order_items", "coin_type_id")
    op.drop_column("order_items", "item_kind")

    op.drop_column("products", "source_ref_id")
    op.drop_column("products", "source_ref_type")
    op.drop_column("products", "status")
    op.drop_column("products", "cost_basis_usd")
    op.drop_column("products", "is_used")

    sa.Enum(name="orderitemkind_enum").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="productstatus_enum").drop(op.get_bind(), checkfirst=True)
