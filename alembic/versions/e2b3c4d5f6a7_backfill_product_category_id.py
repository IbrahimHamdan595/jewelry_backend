"""backfill products.category_id from legacy category string (case-insensitive name_en match)

Revision ID: e2b3c4d5f6a7
Revises: d1a2b3c4e5f6
Create Date: 2026-06-15
"""
from alembic import op
import sqlalchemy as sa

revision = "e2b3c4d5f6a7"
down_revision = "d1a2b3c4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE products p
        SET category_id = c.id
        FROM categories c
        WHERE p.category_id IS NULL
          AND p.category IS NOT NULL
          AND p.category <> ''
          AND lower(p.category) = lower(c.name_en)
        """
    )


def downgrade() -> None:
    # Backfill is not safely reversible per-row (cannot distinguish category_ids
    # set by this migration vs. by users). No-op.
    pass
