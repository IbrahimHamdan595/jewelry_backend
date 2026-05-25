"""audit B2: stock_takes + stock_take_lines + workflow enums

Revision ID: be276347fff9
Revises: b66a1a3f957f
Create Date: 2026-05-25 23:30:00.000000

These are WORKFLOW tables, not audit tables. They legitimately mutate
(status transitions, draft line edits). No A2-style append-only triggers
applied. The audit guarantee for inventory mutations lives in the chained
ManualAdjustment events that APPROVED lines emit through the existing
adjustments path — see app/api/stock_takes.py and the
apply_manual_adjustment_core helper.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "be276347fff9"
down_revision: Union[str, None] = "b66a1a3f957f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Native PG enums created automatically by SQLAlchemy when it sees these
# in the column definitions below. SQLite no-ops the ENUM type and stores
# the value as a CHECK-constrained string.
_status_enum = sa.Enum(
    "DRAFT", "SUBMITTED", "CLOSED", name="stocktakestatus_enum"
)
_resolution_enum = sa.Enum(
    "PENDING", "APPROVED", "REJECTED", "NO_VARIANCE",
    name="stocktakelineresolution_enum",
)
_ref_type_enum = sa.Enum(
    "COIN_STOCK", "OUNCE_STOCK", name="stocktakereftype_enum"
)


def upgrade() -> None:
    op.create_table(
        "stock_takes",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_by_user_id", sa.String(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", _status_enum, nullable=False, server_default="DRAFT"),
        sa.Column("notes", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["started_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_stock_takes_status", "stock_takes", ["status"])
    op.create_index("ix_stock_takes_started_at", "stock_takes", ["started_at"])

    op.create_table(
        "stock_take_lines",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("stock_take_id", sa.String(), nullable=False),
        sa.Column("ref_type", _ref_type_enum, nullable=False),
        sa.Column("ref_id", sa.String(), nullable=False),
        sa.Column("counted_qty", sa.Integer(), nullable=False),
        sa.Column("expected_qty_at_submit", sa.Integer(), nullable=True),
        sa.Column("variance", sa.Integer(), nullable=True),
        sa.Column(
            "resolution",
            _resolution_enum,
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("rejection_reason", sa.String(), nullable=True),
        sa.Column("adjustment_id", sa.String(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by_user_id", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["stock_take_id"], ["stock_takes.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["adjustment_id"], ["manual_adjustments.id"]),
        sa.ForeignKeyConstraint(["resolved_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_stock_take_lines_parent", "stock_take_lines", ["stock_take_id"])
    op.create_index(
        "ix_stock_take_lines_resolution", "stock_take_lines", ["resolution"]
    )
    op.create_index(
        "uq_stock_take_lines_unique_per_take",
        "stock_take_lines",
        ["stock_take_id", "ref_type", "ref_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_stock_take_lines_unique_per_take", table_name="stock_take_lines")
    op.drop_index("ix_stock_take_lines_resolution", table_name="stock_take_lines")
    op.drop_index("ix_stock_take_lines_parent", table_name="stock_take_lines")
    op.drop_table("stock_take_lines")

    op.drop_index("ix_stock_takes_started_at", table_name="stock_takes")
    op.drop_index("ix_stock_takes_status", table_name="stock_takes")
    op.drop_table("stock_takes")

    if op.get_bind().dialect.name == "postgresql":
        # Tables are gone; drop the now-orphan native enum types.
        op.execute("DROP TYPE IF EXISTS stocktakereftype_enum")
        op.execute("DROP TYPE IF EXISTS stocktakelineresolution_enum")
        op.execute("DROP TYPE IF EXISTS stocktakestatus_enum")
