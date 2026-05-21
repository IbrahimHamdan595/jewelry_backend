"""inventory phase 2: coin_types and ounce_types catalogs

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-05-20 22:10:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d8e9f0a1b2c3"
down_revision = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    marginmode = sa.Enum("USD", "PERCENT", name="marginmode_enum")
    marginmode.create(op.get_bind(), checkfirst=True)

    for table in ("coin_types", "ounce_types"):
        op.create_table(
            table,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("code", sa.String(), nullable=False),
            sa.Column("name_en", sa.String(), nullable=False),
            sa.Column("name_ar", sa.String(), nullable=False, server_default=""),
            sa.Column("karat", postgresql.ENUM(name="karat_enum", create_type=False), nullable=False),
            sa.Column("weight_grams", sa.Numeric(10, 3), nullable=False),
            sa.Column("markup_per_gram", sa.Numeric(10, 4), nullable=False, server_default="0"),
            sa.Column("margin_mode", postgresql.ENUM(name="marginmode_enum", create_type=False), nullable=False),
            sa.Column("margin_value", sa.Numeric(12, 2), nullable=False, server_default="0"),
            sa.Column("on_hand_qty", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("min_stock_qty", sa.Integer(), nullable=True),
            sa.Column("photo_url", sa.String(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("code"),
        )
        op.create_index(f"ix_{table}_code", table, ["code"])
        op.create_index(f"ix_{table}_is_active", table, ["is_active"])


def downgrade() -> None:
    for table in ("ounce_types", "coin_types"):
        op.drop_index(f"ix_{table}_is_active", table)
        op.drop_index(f"ix_{table}_code", table)
        op.drop_table(table)
    sa.Enum(name="marginmode_enum").drop(op.get_bind(), checkfirst=True)
