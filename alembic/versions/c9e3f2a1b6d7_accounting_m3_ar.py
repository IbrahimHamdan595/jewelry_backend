"""accounting m3 ar

Revision ID: c9e3f2a1b6d7
Revises: b8d2e1f4a5c6
Create Date: 2026-06-03 18:45:00.000000

Module 3 (Accounts Receivable): customers + AR invoices/lines + receipts/
allocations subledger, plus orders.customer_id and a CREDIT payment method.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c9e3f2a1b6d7"
down_revision: Union[str, None] = "b8d2e1f4a5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # CREDIT payment method (Postgres enum; SQLite is a no-op string check).
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE paymentmethod_enum ADD VALUE IF NOT EXISTS 'CREDIT'")

    op.create_table(
        "customers",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("phone", sa.String(), nullable=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("credit_limit", sa.Numeric(18, 2), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.add_column("orders", sa.Column("customer_id", sa.String(), nullable=True))
    op.create_foreign_key("fk_orders_customer", "orders", "customers", ["customer_id"], ["id"])

    op.create_table(
        "ar_invoices",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("invoice_no", sa.String(), nullable=False),
        sa.Column("customer_id", sa.String(), nullable=False),
        sa.Column("order_id", sa.String(), nullable=True),
        sa.Column("invoice_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("subtotal", sa.Numeric(18, 2), nullable=False),
        sa.Column("vat_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("total", sa.Numeric(18, 2), nullable=False),
        sa.Column("amount_paid", sa.Numeric(18, 2), nullable=False),
        sa.Column("status", sa.Enum("OPEN", "PARTIAL", "PAID", "VOID", name="ar_invoice_status_enum"), nullable=False),
        sa.Column("gl_entry_id", sa.String(), nullable=True),
        sa.Column("memo", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["gl_entry_id"], ["gl_journal_entries.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("invoice_no"),
    )
    op.create_index("ix_ar_invoices_customer_status", "ar_invoices", ["customer_id", "status"], unique=False)

    op.create_table(
        "ar_invoice_lines",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("invoice_id", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(18, 2), nullable=False),
        sa.Column("line_total", sa.Numeric(18, 2), nullable=False),
        sa.ForeignKeyConstraint(["invoice_id"], ["ar_invoices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "ar_receipts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("receipt_no", sa.String(), nullable=False),
        sa.Column("customer_id", sa.String(), nullable=False),
        sa.Column("receipt_date", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("payment_system_key", sa.String(), nullable=False),
        sa.Column("unapplied_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("gl_entry_id", sa.String(), nullable=True),
        sa.Column("memo", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(["gl_entry_id"], ["gl_journal_entries.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("receipt_no"),
    )

    op.create_table(
        "ar_receipt_allocations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("receipt_id", sa.String(), nullable=False),
        sa.Column("invoice_id", sa.String(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.ForeignKeyConstraint(["receipt_id"], ["ar_receipts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invoice_id"], ["ar_invoices.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("ar_receipt_allocations")
    op.drop_table("ar_receipts")
    op.drop_index("ix_ar_invoices_customer_status", table_name="ar_invoices")
    op.drop_table("ar_invoice_lines")
    op.drop_table("ar_invoices")
    op.drop_constraint("fk_orders_customer", "orders", type_="foreignkey")
    op.drop_column("orders", "customer_id")
    op.drop_table("customers")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS ar_invoice_status_enum;")
    # NOTE: the CREDIT enum value is not reversible in Postgres; left in place.
