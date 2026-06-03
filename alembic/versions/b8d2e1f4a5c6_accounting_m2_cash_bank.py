"""accounting m2 cash bank

Revision ID: b8d2e1f4a5c6
Revises: a7c1d0e2f3b4
Create Date: 2026-06-03 17:30:00.000000

Module 2 (Cash & Bank): bank_accounts (each backed by a MONEY gl_account),
bank_statement_lines (import + reconciliation; clearing lives here because
gl_journal_line is immutable), and reconciliation sessions.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b8d2e1f4a5c6"
down_revision: Union[str, None] = "a7c1d0e2f3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bank_accounts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("gl_account_id", sa.String(), nullable=False),
        sa.Column("account_type", sa.Enum("CASH", "BANK", "PETTY_CASH", name="bank_account_type_enum"), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("bank_name", sa.String(), nullable=True),
        sa.Column("account_number", sa.String(), nullable=True),
        sa.Column("opening_balance", sa.Numeric(18, 2), nullable=True),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["gl_account_id"], ["gl_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("gl_account_id"),
    )

    op.create_table(
        "reconciliations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("bank_account_id", sa.String(), nullable=False),
        sa.Column("statement_date", sa.Date(), nullable=False),
        sa.Column("statement_balance", sa.Numeric(18, 2), nullable=False),
        sa.Column("gl_balance", sa.Numeric(18, 2), nullable=False),
        sa.Column("cleared_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("difference", sa.Numeric(18, 2), nullable=False),
        sa.Column("status", sa.Enum("OPEN", "COMPLETED", name="reconciliation_status_enum"), nullable=False),
        sa.Column("started_by_user_id", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["bank_account_id"], ["bank_accounts.id"]),
        sa.ForeignKeyConstraint(["started_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "bank_statement_lines",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("bank_account_id", sa.String(), nullable=False),
        sa.Column("stmt_date", sa.Date(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("reference", sa.String(), nullable=True),
        sa.Column("matched_gl_line_id", sa.String(), nullable=True),
        sa.Column("reconciliation_id", sa.String(), nullable=True),
        sa.Column("status", sa.Enum("UNMATCHED", "MATCHED", name="bank_stmt_line_status_enum"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["bank_account_id"], ["bank_accounts.id"]),
        sa.ForeignKeyConstraint(["matched_gl_line_id"], ["gl_journal_lines.id"]),
        sa.ForeignKeyConstraint(["reconciliation_id"], ["reconciliations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bank_stmt_lines_acct_status", "bank_statement_lines",
                    ["bank_account_id", "status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_bank_stmt_lines_acct_status", table_name="bank_statement_lines")
    op.drop_table("bank_statement_lines")
    op.drop_table("reconciliations")
    op.drop_table("bank_accounts")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for enum_name in ("bank_stmt_line_status_enum", "reconciliation_status_enum", "bank_account_type_enum"):
            op.execute(f"DROP TYPE IF EXISTS {enum_name};")
