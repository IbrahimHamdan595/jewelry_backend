"""add karat markups to settings

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-13 00:01:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("settings", sa.Column("markup_k18", sa.Numeric(10, 2), nullable=False, server_default="0"))
    op.add_column("settings", sa.Column("markup_k21", sa.Numeric(10, 2), nullable=False, server_default="0"))
    op.add_column("settings", sa.Column("markup_k24", sa.Numeric(10, 2), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("settings", "markup_k24")
    op.drop_column("settings", "markup_k21")
    op.drop_column("settings", "markup_k18")
