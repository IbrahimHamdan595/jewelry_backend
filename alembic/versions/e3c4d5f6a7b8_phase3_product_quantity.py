"""phase 3: product quantity — on_hand_qty + min_stock_qty on products

Converts atomic 1-of-1 products to stocked-by-quantity. Backfill derives the
initial on_hand_qty from the existing status:
    AVAILABLE / RESERVED → 1   (physically on hand)
    SOLD / MELTED / INACTIVE → 0

Revision ID: e3c4d5f6a7b8
Revises: d2b3c4e5f6a7
Create Date: 2026-06-01 15:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "e3c4d5f6a7b8"
down_revision = "d2b3c4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add nullable first, backfill, then enforce NOT NULL + default.
    op.add_column("products", sa.Column("on_hand_qty", sa.Integer(), nullable=True))
    op.add_column("products", sa.Column("min_stock_qty", sa.Integer(), nullable=True))

    op.execute(
        "UPDATE products SET on_hand_qty = CASE "
        "WHEN status IN ('AVAILABLE', 'RESERVED') THEN 1 ELSE 0 END"
    )

    op.alter_column("products", "on_hand_qty", nullable=False, server_default="1")


def downgrade() -> None:
    op.drop_column("products", "min_stock_qty")
    op.drop_column("products", "on_hand_qty")
