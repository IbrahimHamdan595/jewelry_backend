"""phase 2: order-level discount + Settings.max_discount_percent

Revision ID: d2b3c4e5f6a7
Revises: c1a2b3d4e5f6
Create Date: 2026-06-01 14:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "d2b3c4e5f6a7"
down_revision = "c1a2b3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("discount_percent", sa.Numeric(5, 2), nullable=False, server_default="0"),
    )
    op.add_column(
        "orders",
        sa.Column("discount_amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
    )
    op.add_column(
        "settings",
        sa.Column("max_discount_percent", sa.Numeric(5, 2), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("settings", "max_discount_percent")
    op.drop_column("orders", "discount_amount")
    op.drop_column("orders", "discount_percent")
