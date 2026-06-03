"""accounting m6 tax

Revision ID: e1a5b4c3d2f9
Revises: d0f4a3b2c1e8
Create Date: 2026-06-04 09:30:00.000000

Module 6 (Tax/VAT): tax_codes table + input-VAT columns on vendor_bills
(subtotal, vat_amount, tax_code_id). The 3 standard codes are data, seeded
via POST /accounting/tax/seed-codes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e1a5b4c3d2f9"
down_revision: Union[str, None] = "d0f4a3b2c1e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tax_codes",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("rate", sa.Numeric(5, 2), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    # server_default 0 backfills existing M5 bills (which had no VAT split).
    op.add_column("vendor_bills", sa.Column("subtotal", sa.Numeric(18, 2), nullable=False, server_default=sa.text("0")))
    op.add_column("vendor_bills", sa.Column("vat_amount", sa.Numeric(18, 2), nullable=False, server_default=sa.text("0")))
    op.add_column("vendor_bills", sa.Column("tax_code_id", sa.String(), nullable=True))
    op.create_foreign_key("fk_vendor_bills_tax_code", "vendor_bills", "tax_codes", ["tax_code_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_vendor_bills_tax_code", "vendor_bills", type_="foreignkey")
    op.drop_column("vendor_bills", "tax_code_id")
    op.drop_column("vendor_bills", "vat_amount")
    op.drop_column("vendor_bills", "subtotal")
    op.drop_table("tax_codes")
