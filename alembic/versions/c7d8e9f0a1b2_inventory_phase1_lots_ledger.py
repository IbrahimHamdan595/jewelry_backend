"""inventory phase 1: gold lots, lot consumptions, inventory ledger, manual adjustments

Revision ID: c7d8e9f0a1b2
Revises: b2c3d4e5f6a7
Create Date: 2026-05-20 21:40:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c7d8e9f0a1b2"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    lotsource = sa.Enum("BUYBACK", "MELT", "SUPPLIER", "SEED", "ADJUSTMENT", name="lotsource_enum")
    adj_target = sa.Enum("LOT", "PRODUCT", "COIN_STOCK", "OUNCE_STOCK", name="adjustmenttarget_enum")
    adj_reason = sa.Enum("LOSS", "THEFT", "GIFT", "SAMPLE", "CORRECTION", name="adjustmentreason_enum")
    lotsource.create(op.get_bind(), checkfirst=True)
    adj_target.create(op.get_bind(), checkfirst=True)
    adj_reason.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "gold_lots",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("karat", postgresql.ENUM(name="karat_enum", create_type=False), nullable=False),
        sa.Column("weight_grams", sa.Numeric(10, 3), nullable=False),
        sa.Column("weight_remaining_grams", sa.Numeric(10, 3), nullable=False),
        sa.Column("source", postgresql.ENUM(name="lotsource_enum", create_type=False), nullable=False),
        sa.Column("source_ref_type", sa.String(), nullable=True),
        sa.Column("source_ref_id", sa.String(), nullable=True),
        sa.Column("cost_basis_usd", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("acquired_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("is_depleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_gold_lots_karat", "gold_lots", ["karat"])
    op.create_index("ix_gold_lots_is_depleted", "gold_lots", ["is_depleted"])

    op.create_table(
        "gold_lot_consumptions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("lot_id", sa.String(), nullable=False),
        sa.Column("grams", sa.Numeric(10, 3), nullable=False),
        sa.Column("cost_basis_consumed_usd", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("ref_type", sa.String(), nullable=False),
        sa.Column("ref_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("created_by_user_id", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["lot_id"], ["gold_lots.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
    )
    op.create_index("ix_lot_consumptions_lot", "gold_lot_consumptions", ["lot_id"])
    op.create_index("ix_lot_consumptions_ref", "gold_lot_consumptions", ["ref_type", "ref_id"])

    op.create_table(
        "inventory_ledger",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("actor_user_id", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("ref_type", sa.String(), nullable=False),
        sa.Column("ref_id", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
    )
    op.create_index("ix_inventory_ledger_event_type", "inventory_ledger", ["event_type"])
    op.create_index("ix_inventory_ledger_ref", "inventory_ledger", ["ref_type", "ref_id"])
    op.create_index("ix_inventory_ledger_occurred_at", "inventory_ledger", ["occurred_at"])

    op.create_table(
        "manual_adjustments",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("target_type", postgresql.ENUM(name="adjustmenttarget_enum", create_type=False), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("delta", sa.Numeric(12, 3), nullable=False),
        sa.Column("reason", postgresql.ENUM(name="adjustmentreason_enum", create_type=False), nullable=False),
        sa.Column("notes", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("actor_user_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
    )
    op.create_index("ix_manual_adjustments_target", "manual_adjustments", ["target_type", "target_id"])


def downgrade() -> None:
    op.drop_index("ix_manual_adjustments_target", "manual_adjustments")
    op.drop_table("manual_adjustments")

    op.drop_index("ix_inventory_ledger_occurred_at", "inventory_ledger")
    op.drop_index("ix_inventory_ledger_ref", "inventory_ledger")
    op.drop_index("ix_inventory_ledger_event_type", "inventory_ledger")
    op.drop_table("inventory_ledger")

    op.drop_index("ix_lot_consumptions_ref", "gold_lot_consumptions")
    op.drop_index("ix_lot_consumptions_lot", "gold_lot_consumptions")
    op.drop_table("gold_lot_consumptions")

    op.drop_index("ix_gold_lots_is_depleted", "gold_lots")
    op.drop_index("ix_gold_lots_karat", "gold_lots")
    op.drop_table("gold_lots")

    sa.Enum(name="adjustmentreason_enum").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="adjustmenttarget_enum").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="lotsource_enum").drop(op.get_bind(), checkfirst=True)
