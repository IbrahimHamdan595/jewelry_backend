"""Lebanese CoA renumber — change gl_accounts.code to standard codes (keyed by system_key).

Renumber-in-place (design 2026-06-07 §5). system_key unchanged. New codes are
6-digit, disjoint from the old 4-digit codes, so there are no intermediate
UNIQUE collisions. Local Docker PG only — never Neon.
"""
from alembic import op
import sqlalchemy as sa

revision = "c4e5f6a7b8d0"
down_revision = "b3f4a1c2d5e6"
branch_labels = None
depends_on = None

RENUMBER = {
    "CASH": "530001", "CASH_LBP": "530011", "BANK": "512201",
    "METAL_INVENTORY": "370011", "PRODUCT_INVENTORY": "370012", "METAL_CLEARING": "370019",
    "AR": "411111", "VAT_RECEIVABLE": "442611", "AP": "401101", "METAL_AP": "401102",
    "VAT_PAYABLE": "442701", "CUSTOMER_DEPOSITS": "419101", "VENDOR_AP": "461901",
    "OPENING_BALANCE_EQUITY": "101901", "RETAINED_EARNINGS": "101401",
    "SALES_REVENUE": "701000", "MAKING_CHARGE_REVENUE": "713000", "FX_GAIN": "775100",
    "METAL_COGS": "611701", "MAKING_COGS": "611702", "ADJUSTMENT_EXPENSE": "655300",
    "RENT_EXPENSE": "626310", "UTILITIES_EXPENSE": "626340", "SALARIES_EXPENSE": "631100",
    "MARKETING_EXPENSE": "626930", "BANK_CHARGES_EXPENSE": "673900", "OFFICE_EXPENSE": "626940",
    "MISC_EXPENSE": "626991", "FX_LOSS": "675100",
}

OLD = {
    "CASH": "1000", "CASH_LBP": "1010", "BANK": "1020", "AR": "1100",
    "METAL_INVENTORY": "1200", "PRODUCT_INVENTORY": "1210", "METAL_CLEARING": "1250",
    "VAT_RECEIVABLE": "1300", "AP": "2000", "METAL_AP": "2100", "VAT_PAYABLE": "2200",
    "CUSTOMER_DEPOSITS": "2300", "VENDOR_AP": "2400",
    "OPENING_BALANCE_EQUITY": "3000", "RETAINED_EARNINGS": "3100",
    "SALES_REVENUE": "4000", "MAKING_CHARGE_REVENUE": "4100", "FX_GAIN": "4900",
    "METAL_COGS": "5000", "MAKING_COGS": "5100", "ADJUSTMENT_EXPENSE": "5200",
    "FX_LOSS": "6900", "RENT_EXPENSE": "6000", "UTILITIES_EXPENSE": "6100",
    "SALARIES_EXPENSE": "6200", "MARKETING_EXPENSE": "6300", "BANK_CHARGES_EXPENSE": "6400",
    "OFFICE_EXPENSE": "6500", "MISC_EXPENSE": "6800",
}

_accounts = sa.table("gl_accounts", sa.column("code", sa.String), sa.column("system_key", sa.String))


def _apply(mapping):
    for key, code in mapping.items():
        op.execute(
            _accounts.update()
            .where(_accounts.c.system_key == op.inline_literal(key))
            .values(code=op.inline_literal(code))
        )


def upgrade():
    _apply(RENUMBER)


def downgrade():
    _apply(OLD)
