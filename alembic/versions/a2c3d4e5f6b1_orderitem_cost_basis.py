"""orderitem cost basis snapshot

Captures the metal COGS of each sale line at checkout so gross profit /
margin / profit-per-gram are queryable without the (dormant) GL.

Revision ID: a2c3d4e5f6b1
Revises: e1a5b4c3d2f9
"""
import sqlalchemy as sa
from alembic import op

revision = "a2c3d4e5f6b1"
down_revision = "e1a5b4c3d2f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("order_items", sa.Column("cost_basis_usd", sa.Numeric(12, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("order_items", "cost_basis_usd")
