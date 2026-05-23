"""inventory phase 3: add K22 karat; walk-in buybacks; buyback defaults on settings

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-05-20 22:40:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e9f0a1b2c3d4"
down_revision = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add K22 to existing karat_enum. ALTER TYPE ADD VALUE must run outside
    # a transaction block on PostgreSQL.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE karat_enum ADD VALUE IF NOT EXISTS 'K22' AFTER 'K21'")

    # New enums for buyback
    buyback_kind = sa.Enum(
        "PURE_GOLD", "COIN", "OUNCE", "USED_PRODUCT", name="buybackkind_enum",
    )
    buyback_margin_mode = sa.Enum(
        "USD_PER_GRAM", "PERCENT", name="buybackmarginmode_enum",
    )
    buyback_price_mode = sa.Enum("FORMULA", "MANUAL", name="buybackpricemode_enum")
    buyback_kind.create(op.get_bind(), checkfirst=True)
    buyback_margin_mode.create(op.get_bind(), checkfirst=True)
    buyback_price_mode.create(op.get_bind(), checkfirst=True)

    # Settings: buyback defaults
    op.add_column(
        "settings",
        sa.Column(
            "default_buyback_margin_mode",
            postgresql.ENUM(name="buybackmarginmode_enum", create_type=False),
            nullable=False,
            server_default="USD_PER_GRAM",
        ),
    )
    op.add_column(
        "settings",
        sa.Column(
            "default_buyback_margin_value",
            sa.Numeric(12, 4),
            nullable=False,
            server_default="2",
        ),
    )
    op.add_column(
        "settings",
        sa.Column(
            "buyback_rate_drift_pct_max",
            sa.Numeric(5, 2),
            nullable=False,
            server_default="2",
        ),
    )

    op.create_table(
        "walkin_buybacks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("seller_name", sa.String(), nullable=False),
        sa.Column("seller_phone", sa.String(), nullable=False),
        sa.Column("cashier_id", sa.String(), nullable=False),
        sa.Column("kind", postgresql.ENUM(name="buybackkind_enum", create_type=False), nullable=False),
        sa.Column("result_lot_id", sa.String(), nullable=True),
        sa.Column("coin_type_id", sa.String(), nullable=True),
        sa.Column("ounce_type_id", sa.String(), nullable=True),
        sa.Column("product_id", sa.String(), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=True),
        sa.Column("weight_grams", sa.Numeric(10, 3), nullable=True),
        sa.Column("karat", postgresql.ENUM(name="karat_enum", create_type=False), nullable=True),
        sa.Column("buy_price_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("gold_rate_at_buy", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "buyback_margin_mode",
            postgresql.ENUM(name="buybackmarginmode_enum", create_type=False),
            nullable=True,
        ),
        sa.Column("buyback_margin_value", sa.Numeric(12, 4), nullable=True),
        sa.Column(
            "price_mode",
            postgresql.ENUM(name="buybackpricemode_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["cashier_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["result_lot_id"], ["gold_lots.id"]),
        sa.ForeignKeyConstraint(["coin_type_id"], ["coin_types.id"]),
        sa.ForeignKeyConstraint(["ounce_type_id"], ["ounce_types.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
    )
    op.create_index("ix_walkin_buybacks_occurred_at", "walkin_buybacks", ["occurred_at"])
    op.create_index("ix_walkin_buybacks_kind", "walkin_buybacks", ["kind"])
    op.create_index("ix_walkin_buybacks_cashier", "walkin_buybacks", ["cashier_id"])


def downgrade() -> None:
    op.drop_index("ix_walkin_buybacks_cashier", "walkin_buybacks")
    op.drop_index("ix_walkin_buybacks_kind", "walkin_buybacks")
    op.drop_index("ix_walkin_buybacks_occurred_at", "walkin_buybacks")
    op.drop_table("walkin_buybacks")

    op.drop_column("settings", "buyback_rate_drift_pct_max")
    op.drop_column("settings", "default_buyback_margin_value")
    op.drop_column("settings", "default_buyback_margin_mode")

    sa.Enum(name="buybackpricemode_enum").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="buybackmarginmode_enum").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="buybackkind_enum").drop(op.get_bind(), checkfirst=True)

    # NOTE: Postgres does not support removing an enum value once added.
    # The 'K22' value remains in karat_enum after a downgrade; this is harmless.
