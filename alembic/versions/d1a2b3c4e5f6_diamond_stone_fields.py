"""diamond stone fields — stone columns on products/order_items + stone GL accounts

Revision ID: d1a2b3c4e5f6
Revises: c4e5f6a7b8d0
Create Date: 2026-06-13

Adds stone-related metadata columns to products and order_items, and inserts two
new system GL accounts (STONE_INVENTORY, STONE_COGS) used by the diamond auto-
posting bridge (design: diamond-products plan §3).
"""
from __future__ import annotations

from uuid import uuid4

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "d1a2b3c4e5f6"
down_revision: str = "c4e5f6a7b8d0"
branch_labels = None
depends_on = None


def _uid() -> str:
    """Match the app-layer id convention: uuid4().hex (32 lowercase hex chars, no dashes)."""
    return uuid4().hex


def upgrade() -> None:
    # ── 1. stone columns on products ─────────────────────────────────────────
    op.add_column("products", sa.Column("stone_value_usd", sa.Numeric(12, 2), nullable=True))
    op.add_column("products", sa.Column("stone_cost_usd",  sa.Numeric(12, 2), nullable=True))
    op.add_column("products", sa.Column("stone_carats",    sa.Numeric(8, 3),  nullable=True))
    op.add_column("products", sa.Column("stone_count",     sa.Integer(),      nullable=True))
    op.add_column("products", sa.Column("stone_cert",      sa.String(),       nullable=True))
    op.add_column("products", sa.Column("stone_note",      sa.String(),       nullable=True))

    # ── 2. stone snapshot columns on order_items ──────────────────────────────
    op.add_column("order_items", sa.Column("stone_value_at_sale", sa.Numeric(12, 2), nullable=True))
    op.add_column("order_items", sa.Column("stone_cost_at_sale",  sa.Numeric(12, 2), nullable=True))

    # ── 3. insert stone GL accounts (idempotent — skip if system_key exists) ──
    # gl_accounts NOT-NULL columns (no server defaults that we must supply):
    #   id, code, name, type, denomination, normal_balance, is_active
    # Enum string values (as stored in PG):
    #   type:           ASSET | LIABILITY | EQUITY | INCOME | EXPENSE
    #   denomination:   MONEY | METAL | DUAL
    #   normal_balance: DEBIT | CREDIT
    bind = op.get_bind()

    existing = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT system_key FROM gl_accounts WHERE system_key IN ('STONE_INVENTORY', 'STONE_COGS')")
        )
    }

    if "STONE_INVENTORY" not in existing:
        bind.execute(
            sa.text(
                "INSERT INTO gl_accounts "
                "(id, code, name, type, denomination, normal_balance, currency, system_key, is_active) "
                "VALUES (:id, :code, :name, :type, :denomination, :normal_balance, :currency, :system_key, :is_active)"
            ),
            {
                "id":             _uid(),
                "code":           "370013",
                "name":           "Stone Inventory",
                "type":           "ASSET",
                "denomination":   "MONEY",
                "normal_balance": "DEBIT",
                "currency":       "USD",
                "system_key":     "STONE_INVENTORY",
                "is_active":      True,
            },
        )

    if "STONE_COGS" not in existing:
        bind.execute(
            sa.text(
                "INSERT INTO gl_accounts "
                "(id, code, name, type, denomination, normal_balance, currency, system_key, is_active) "
                "VALUES (:id, :code, :name, :type, :denomination, :normal_balance, :currency, :system_key, :is_active)"
            ),
            {
                "id":             _uid(),
                "code":           "611703",
                "name":           "Stone COGS",
                "type":           "EXPENSE",
                "denomination":   "MONEY",
                "normal_balance": "DEBIT",
                "currency":       "USD",
                "system_key":     "STONE_COGS",
                "is_active":      True,
            },
        )


def downgrade() -> None:
    # ── 1. delete stone GL accounts ───────────────────────────────────────────
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM gl_accounts WHERE system_key IN ('STONE_INVENTORY', 'STONE_COGS')")
    )

    # ── 2. drop order_items stone columns ─────────────────────────────────────
    op.drop_column("order_items", "stone_cost_at_sale")
    op.drop_column("order_items", "stone_value_at_sale")

    # ── 3. drop products stone columns ────────────────────────────────────────
    op.drop_column("products", "stone_note")
    op.drop_column("products", "stone_cert")
    op.drop_column("products", "stone_count")
    op.drop_column("products", "stone_carats")
    op.drop_column("products", "stone_cost_usd")
    op.drop_column("products", "stone_value_usd")
