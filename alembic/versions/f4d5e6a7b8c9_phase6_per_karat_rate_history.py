"""phase 6: per-karat columns on gold_rate_history + one-time derived backfill

Revision ID: f4d5e6a7b8c9
Revises: e3c4d5f6a7b8
Create Date: 2026-06-02 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "f4d5e6a7b8c9"
down_revision = "e3c4d5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("gold_rate_history", sa.Column("rate_22k", sa.Numeric(10, 2), nullable=True))
    op.add_column("gold_rate_history", sa.Column("rate_21k", sa.Numeric(10, 2), nullable=True))
    op.add_column("gold_rate_history", sa.Column("rate_18k", sa.Numeric(10, 2), nullable=True))
    op.add_column(
        "gold_rate_history",
        sa.Column("per_karat_backfilled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # One-time backfill of historical rows from rate_24k via purity multipliers
    # (KARAT_PURITY: 22K=0.917, 21K=0.875, 18K=0.750). Flagged derived so the
    # series is honest about which points were actually polled per-karat.
    op.execute(
        "UPDATE gold_rate_history SET "
        "rate_22k = ROUND(rate_24k * 0.917, 2), "
        "rate_21k = ROUND(rate_24k * 0.875, 2), "
        "rate_18k = ROUND(rate_24k * 0.750, 2), "
        "per_karat_backfilled = true "
        "WHERE rate_22k IS NULL"
    )


def downgrade() -> None:
    op.drop_column("gold_rate_history", "per_karat_backfilled")
    op.drop_column("gold_rate_history", "rate_18k")
    op.drop_column("gold_rate_history", "rate_21k")
    op.drop_column("gold_rate_history", "rate_22k")
