"""users.token_version — session invalidation (NEX-54)

Revision ID: e65d977573d3
Revises: e2b3c4d5f6a7
Create Date: 2026-10-04

Every JWT carries the token_version it was issued under; get_current_user
refuses any other. Bumping the column (password change, admin force-logout)
ends all of that user's sessions at once.

Additive and backward compatible: NOT NULL with a server default of 0, so
existing rows are filled and code that predates the column keeps inserting
users without naming it. A constant default makes this a metadata-only
change on Postgres 11+ — no table rewrite.

DEPLOY ORDER: apply this BEFORE the code that maps the column runs. The User
model selects token_version on every authenticated request, so new code on
an un-migrated database fails every one of them.
"""
from alembic import op
import sqlalchemy as sa

revision = "e65d977573d3"
down_revision = "e2b3c4d5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("users", "token_version")
