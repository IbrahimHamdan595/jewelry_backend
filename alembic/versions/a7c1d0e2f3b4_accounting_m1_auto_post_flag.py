"""accounting m1 auto post flag

Revision ID: a7c1d0e2f3b4
Revises: f7a1e19327ca
Create Date: 2026-06-03 16:10:00.000000

Module 1 (auto-posting bridge): master switch on settings to enable real-time
GL auto-posting. Default OFF so operations behave exactly as before until
accounting is set up. The two new system accounts (METAL_CLEARING,
ADJUSTMENT_EXPENSE) are data, seeded idempotently via POST /accounting/seed-coa
— no schema change needed for them.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a7c1d0e2f3b4"
down_revision: Union[str, None] = "f7a1e19327ca"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "settings",
        sa.Column("accounting_auto_post_enabled", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("settings", "accounting_auto_post_enabled")
