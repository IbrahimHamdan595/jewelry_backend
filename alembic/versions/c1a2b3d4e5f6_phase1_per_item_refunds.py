"""phase 1: per-item refunds — order_items refund columns + PARTIALLY_REFUNDED status

Revision ID: c1a2b3d4e5f6
Revises: be276347fff9
Create Date: 2026-06-01 13:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "c1a2b3d4e5f6"
down_revision = "be276347fff9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add PARTIALLY_REFUNDED to the existing orderstatus_enum. ALTER TYPE ADD
    # VALUE must run outside a transaction block on PostgreSQL.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE orderstatus_enum ADD VALUE IF NOT EXISTS "
            "'PARTIALLY_REFUNDED' AFTER 'REFUNDED'"
        )

    # Per-item refund tracking on order_items.
    op.add_column(
        "order_items",
        sa.Column("refunded_qty", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "order_items",
        sa.Column(
            "refunded_amount",
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "order_items",
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("order_items", "refunded_at")
    op.drop_column("order_items", "refunded_amount")
    op.drop_column("order_items", "refunded_qty")
    # NOTE: Postgres does not support removing an enum value once added. The
    # 'PARTIALLY_REFUNDED' value remains in orderstatus_enum after a downgrade;
    # this is harmless (no row references it once the columns are gone).
