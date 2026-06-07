import pytest
from sqlalchemy import select

from app.core.coa_seed import seed_chart_of_accounts, SYSTEM_ACCOUNTS
from app.models import GLAccount

# system_key -> expected standard Lebanese code (renumber map, design §5)
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


@pytest.mark.asyncio
async def test_existing_accounts_use_standard_codes(db):
    await seed_chart_of_accounts(db)
    rows = (await db.execute(select(GLAccount))).scalars().all()
    by_key = {a.system_key: a.code for a in rows}
    for key, code in RENUMBER.items():
        assert by_key[key] == code, f"{key} should be {code}, got {by_key.get(key)}"


def test_codes_unique_in_seed():
    codes = [t[0] for t in SYSTEM_ACCOUNTS]
    assert len(codes) == len(set(codes)), "duplicate codes in SYSTEM_ACCOUNTS"


def _load_renumber_migration():
    import importlib.util
    import pathlib
    path = (pathlib.Path(__file__).resolve().parent.parent
            / "alembic" / "versions" / "c4e5f6a7b8d0_lebanese_coa_renumber.py")
    spec = importlib.util.spec_from_file_location("coa_renumber_mig", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_map_matches_seed():
    # The migration's RENUMBER must cover every renumbered account and be 1:1.
    mod = _load_renumber_migration()
    MIG, OLD = mod.RENUMBER, mod.OLD
    assert set(MIG) == set(RENUMBER), "migration RENUMBER must cover the renumber map"
    assert set(OLD) == set(RENUMBER), "migration OLD (downgrade) must cover the renumber map"
    assert MIG == RENUMBER, "migration RENUMBER must match the test/seed renumber map"
    assert len(set(MIG.values())) == len(MIG), "new codes must be unique"
    assert len(set(OLD.values())) == len(OLD), "old codes must be unique"
