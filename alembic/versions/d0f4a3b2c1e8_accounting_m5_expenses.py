"""accounting m5 expenses

Revision ID: d0f4a3b2c1e8
Revises: c9e3f2a1b6d7
Create Date: 2026-06-03 19:50:00.000000

Module 5 (Expenses & Purchasing): vendor bills + lines + payments + allocations.
The VENDOR_AP and opex expense accounts are data, seeded idempotently via
POST /accounting/seed-coa — no schema change for them.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d0f4a3b2c1e8"
down_revision: Union[str, None] = "c9e3f2a1b6d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "vendor_bills",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("bill_no", sa.String(), nullable=False),
        sa.Column("vendor_name", sa.String(), nullable=False),
        sa.Column("supplier_id", sa.String(), nullable=True),
        sa.Column("bill_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("total", sa.Numeric(18, 2), nullable=False),
        sa.Column("amount_paid", sa.Numeric(18, 2), nullable=False),
        sa.Column("status", sa.Enum("OPEN", "PARTIAL", "PAID", "VOID", name="vendor_bill_status_enum"), nullable=False),
        sa.Column("payment_system_key", sa.String(), nullable=True),
        sa.Column("gl_entry_id", sa.String(), nullable=True),
        sa.Column("memo", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"]),
        sa.ForeignKeyConstraint(["gl_entry_id"], ["gl_journal_entries.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bill_no"),
    )
    op.create_index("ix_vendor_bills_vendor_status", "vendor_bills", ["vendor_name", "status"], unique=False)

    op.create_table(
        "vendor_bill_lines",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("bill_id", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("expense_account_id", sa.String(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.ForeignKeyConstraint(["bill_id"], ["vendor_bills.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["expense_account_id"], ["gl_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "vendor_payments",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("payment_no", sa.String(), nullable=False),
        sa.Column("vendor_name", sa.String(), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("payment_system_key", sa.String(), nullable=False),
        sa.Column("unapplied_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("gl_entry_id", sa.String(), nullable=True),
        sa.Column("memo", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["gl_entry_id"], ["gl_journal_entries.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("payment_no"),
    )

    op.create_table(
        "vendor_payment_allocations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("payment_id", sa.String(), nullable=False),
        sa.Column("bill_id", sa.String(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.ForeignKeyConstraint(["payment_id"], ["vendor_payments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["bill_id"], ["vendor_bills.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("vendor_payment_allocations")
    op.drop_table("vendor_payments")
    op.drop_table("vendor_bill_lines")
    op.drop_index("ix_vendor_bills_vendor_status", table_name="vendor_bills")
    op.drop_table("vendor_bills")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS vendor_bill_status_enum;")
