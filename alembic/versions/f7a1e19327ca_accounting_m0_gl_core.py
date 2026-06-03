"""accounting m0 gl core

Revision ID: f7a1e19327ca
Revises: f4d5e6a7b8c9
Create Date: 2026-06-03 13:33:26.078056

Module 0 (GL Core). Creates the chart-of-accounts + double-entry journal tables
(append-only, hash-chained), the per-day entry-number sequence, and the GL chain
head (seeded with GENESIS). Extends role_enum with ACCOUNTANT/MANAGER and installs
A2 append-only triggers (Postgres only) on the GL audit tables, reusing the
audit_block_mutation() function created in migration e0a8bacf7474.

NOTE: autogenerate also surfaced pre-existing NOT-NULL drift on unrelated tables
(categories/coin_types/gold_lots/...); those alter_column lines were intentionally
removed — they are not part of Module 0 and would silently tighten old tables.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f7a1e19327ca'
down_revision: Union[str, None] = 'f4d5e6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'gl_accounts',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('code', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('type', sa.Enum('ASSET', 'LIABILITY', 'EQUITY', 'INCOME', 'EXPENSE', name='gl_account_type_enum'), nullable=False),
        sa.Column('parent_id', sa.String(), nullable=True),
        sa.Column('denomination', sa.Enum('MONEY', 'METAL', 'DUAL', name='gl_denomination_enum'), nullable=False),
        sa.Column('currency', sa.String(), nullable=True),
        sa.Column('karat', sa.String(), nullable=True),
        sa.Column('system_key', sa.String(), nullable=True),
        sa.Column('normal_balance', sa.Enum('DEBIT', 'CREDIT', name='gl_normal_balance_enum'), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['parent_id'], ['gl_accounts.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
        sa.UniqueConstraint('system_key'),
    )
    op.create_index('ix_gl_accounts_parent', 'gl_accounts', ['parent_id'], unique=False)
    op.create_index('ix_gl_accounts_type', 'gl_accounts', ['type'], unique=False)

    op.create_table(
        'gl_entry_sequence',
        sa.Column('day_key', sa.String(), nullable=False),
        sa.Column('last_seq', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('day_key'),
    )

    op.create_table(
        'gl_journal_chain_head',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('latest_entry_hash', sa.String(), nullable=False),
        sa.Column('row_count', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'gl_periods',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('year', sa.Integer(), nullable=False),
        sa.Column('period_no', sa.Integer(), nullable=False),
        sa.Column('status', sa.Enum('OPEN', 'CLOSED', name='gl_period_status_enum'), nullable=False),
        sa.Column('closed_by_user_id', sa.String(), nullable=True),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['closed_by_user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('uq_gl_periods_year_period', 'gl_periods', ['year', 'period_no'], unique=True)

    op.create_table(
        'gl_journal_entries',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('entry_no', sa.String(), nullable=False),
        sa.Column('entry_date', sa.Date(), nullable=False),
        sa.Column('period_id', sa.String(), nullable=False),
        sa.Column('memo', sa.String(), nullable=False),
        sa.Column('source_type', sa.String(), nullable=False),
        sa.Column('source_id', sa.String(), nullable=True),
        sa.Column('reverses_entry_id', sa.String(), nullable=True),
        sa.Column('actor_user_id', sa.String(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('prev_hash', sa.String(), nullable=False),
        sa.Column('entry_hash', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['period_id'], ['gl_periods.id'], ),
        sa.ForeignKeyConstraint(['reverses_entry_id'], ['gl_journal_entries.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('entry_hash'),
        sa.UniqueConstraint('entry_no'),
    )
    op.create_index('ix_gl_entries_entry_date', 'gl_journal_entries', ['entry_date'], unique=False)
    op.create_index('ix_gl_entries_period', 'gl_journal_entries', ['period_id'], unique=False)
    op.create_index('ix_gl_entries_source', 'gl_journal_entries', ['source_type', 'source_id'], unique=False)

    op.create_table(
        'gl_journal_lines',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('entry_id', sa.String(), nullable=False),
        sa.Column('line_no', sa.Integer(), nullable=False),
        sa.Column('account_id', sa.String(), nullable=False),
        sa.Column('money_debit', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('money_credit', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('currency', sa.String(), nullable=False),
        sa.Column('fx_rate', sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column('base_debit', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('base_credit', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('metal_debit_grams', sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column('metal_credit_grams', sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column('karat', sa.String(), nullable=True),
        sa.Column('memo', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['gl_accounts.id'], ),
        sa.ForeignKeyConstraint(['entry_id'], ['gl_journal_entries.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_gl_lines_account', 'gl_journal_lines', ['account_id'], unique=False)
    op.create_index('ix_gl_lines_entry', 'gl_journal_lines', ['entry_id'], unique=False)

    bind = op.get_bind()

    # Extend role_enum with the new accounting roles (Postgres only). On SQLite
    # the Role enum is just a string check, so nothing to alter.
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE role_enum ADD VALUE IF NOT EXISTS 'ACCOUNTANT'")
        op.execute("ALTER TYPE role_enum ADD VALUE IF NOT EXISTS 'MANAGER'")

    # Seed the GL chain-head singleton with GENESIS (mirrors the inventory ledger
    # + auth audit chain-head seeds).
    op.execute(
        "INSERT INTO gl_journal_chain_head (id, latest_entry_hash, row_count) "
        "VALUES (1, 'GENESIS', 0)"
    )

    # A2 append-only triggers on the GL audit tables (Postgres only). Reuses the
    # audit_block_mutation() function created in migration e0a8bacf7474.
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER trg_gl_journal_entries_block_update "
            "BEFORE UPDATE ON gl_journal_entries "
            "FOR EACH ROW EXECUTE FUNCTION audit_block_mutation();"
        )
        op.execute(
            "CREATE TRIGGER trg_gl_journal_entries_block_delete "
            "BEFORE DELETE ON gl_journal_entries "
            "FOR EACH ROW EXECUTE FUNCTION audit_block_mutation();"
        )
        op.execute(
            "CREATE TRIGGER trg_gl_journal_lines_block_update "
            "BEFORE UPDATE ON gl_journal_lines "
            "FOR EACH ROW EXECUTE FUNCTION audit_block_mutation();"
        )
        op.execute(
            "CREATE TRIGGER trg_gl_journal_lines_block_delete "
            "BEFORE DELETE ON gl_journal_lines "
            "FOR EACH ROW EXECUTE FUNCTION audit_block_mutation();"
        )
        op.execute(
            "CREATE TRIGGER trg_gl_chain_head_block_delete "
            "BEFORE DELETE ON gl_journal_chain_head "
            "FOR EACH ROW EXECUTE FUNCTION audit_block_mutation();"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for trg, tbl in [
            ("trg_gl_chain_head_block_delete", "gl_journal_chain_head"),
            ("trg_gl_journal_lines_block_delete", "gl_journal_lines"),
            ("trg_gl_journal_lines_block_update", "gl_journal_lines"),
            ("trg_gl_journal_entries_block_delete", "gl_journal_entries"),
            ("trg_gl_journal_entries_block_update", "gl_journal_entries"),
        ]:
            op.execute(f"DROP TRIGGER IF EXISTS {trg} ON {tbl};")
    # NOTE: role_enum ADD VALUE is not reversible in Postgres; the two values are
    # left in place on downgrade (harmless).

    op.drop_index('ix_gl_lines_entry', table_name='gl_journal_lines')
    op.drop_index('ix_gl_lines_account', table_name='gl_journal_lines')
    op.drop_table('gl_journal_lines')
    op.drop_index('ix_gl_entries_source', table_name='gl_journal_entries')
    op.drop_index('ix_gl_entries_period', table_name='gl_journal_entries')
    op.drop_index('ix_gl_entries_entry_date', table_name='gl_journal_entries')
    op.drop_table('gl_journal_entries')
    op.drop_index('uq_gl_periods_year_period', table_name='gl_periods')
    op.drop_table('gl_periods')
    op.drop_table('gl_journal_chain_head')
    op.drop_table('gl_entry_sequence')
    op.drop_index('ix_gl_accounts_type', table_name='gl_accounts')
    op.drop_index('ix_gl_accounts_parent', table_name='gl_accounts')
    op.drop_table('gl_accounts')

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for enum_name in ("gl_account_type_enum", "gl_denomination_enum",
                          "gl_normal_balance_enum", "gl_period_status_enum"):
            op.execute(f"DROP TYPE IF EXISTS {enum_name};")
