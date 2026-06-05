"""fx parity — invoice/bill/receipt/payment fx_rate + FX gain/loss account split

Additive columns (default 1 / USD so existing rows are unchanged), plus a data
migration: repurpose the single 6900 FX_GAIN_LOSS account as FX_LOSS, and add a
separate FX_GAIN (4900, INCOME). Idempotent.

Revision ID: b3f4a1c2d5e6
Revises: a2c3d4e5f6b1
"""
import sqlalchemy as sa
from alembic import op

revision = "b3f4a1c2d5e6"
down_revision = "a2c3d4e5f6b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ar_invoices", sa.Column("fx_rate", sa.Numeric(18, 6), nullable=False, server_default="1"))
    op.add_column("vendor_bills", sa.Column("fx_rate", sa.Numeric(18, 6), nullable=False, server_default="1"))
    op.add_column("ar_receipts", sa.Column("fx_rate", sa.Numeric(18, 6), nullable=False, server_default="1"))
    op.add_column("vendor_payments", sa.Column("currency", sa.String(), nullable=False, server_default="USD"))
    op.add_column("vendor_payments", sa.Column("fx_rate", sa.Numeric(18, 6), nullable=False, server_default="1"))

    # FX account split. Repurpose the existing 6900 as FX_LOSS...
    op.execute("UPDATE gl_accounts SET system_key='FX_LOSS', name='FX Loss' WHERE system_key='FX_GAIN_LOSS'")
    # ...and add FX_GAIN (4900, INCOME) if absent.
    op.execute(
        """
        INSERT INTO gl_accounts (id, code, name, type, denomination, currency, system_key, normal_balance, is_active)
        SELECT 'acct_fx_gain_4900', '4900', 'FX Gain',
               'INCOME'::gl_account_type_enum, 'MONEY'::gl_denomination_enum,
               'USD', 'FX_GAIN', 'CREDIT'::gl_normal_balance_enum, true
        WHERE NOT EXISTS (SELECT 1 FROM gl_accounts WHERE system_key = 'FX_GAIN')
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM gl_accounts WHERE system_key='FX_GAIN'")
    op.execute("UPDATE gl_accounts SET system_key='FX_GAIN_LOSS', name='FX Gain/Loss' WHERE system_key='FX_LOSS'")
    op.drop_column("vendor_payments", "fx_rate")
    op.drop_column("vendor_payments", "currency")
    op.drop_column("ar_receipts", "fx_rate")
    op.drop_column("vendor_bills", "fx_rate")
    op.drop_column("ar_invoices", "fx_rate")
