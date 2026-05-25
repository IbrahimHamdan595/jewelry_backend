"""audit A1: hash-chain columns on inventory_ledger + chain_head

Revision ID: 28b387f25187
Revises: 2406afe136c2
Create Date: 2026-05-25 18:55:42.105937

This migration is purely additive. The new columns on `inventory_ledger`
are nullable so existing rows survive. Audit phase A1.2 backfills them and
tightens to NOT NULL once the chain is populated.

The chain-head row is seeded with the GENESIS sentinel and row_count=0;
the first call to `record()` will discover this row, chain to it, and
advance the head.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '28b387f25187'
down_revision: Union[str, None] = '2406afe136c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Chain columns on inventory_ledger — nullable for now; phase A1.2
    #    populates them and tightens to NOT NULL.
    op.add_column(
        "inventory_ledger",
        sa.Column("prev_hash", sa.String(), nullable=True),
    )
    op.add_column(
        "inventory_ledger",
        sa.Column("entry_hash", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_inventory_ledger_entry_hash",
        "inventory_ledger",
        ["entry_hash"],
        unique=False,
    )

    # 2. Single-row head table. Seeded with GENESIS sentinel so the very first
    #    `record()` call after this migration finds a valid row to lock.
    op.create_table(
        "inventory_ledger_chain_head",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("latest_entry_hash", sa.String(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "INSERT INTO inventory_ledger_chain_head (id, latest_entry_hash, row_count) "
        "VALUES (1, 'GENESIS', 0)"
    )


def downgrade() -> None:
    op.drop_table("inventory_ledger_chain_head")
    op.drop_index("ix_inventory_ledger_entry_hash", table_name="inventory_ledger")
    op.drop_column("inventory_ledger", "entry_hash")
    op.drop_column("inventory_ledger", "prev_hash")
