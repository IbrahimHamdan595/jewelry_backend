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


from app.models import AccountType, NormalBalance

NEW_ACCOUNTS = {
    # system_key: (code, AccountType, NormalBalance)
    "TELECOM_EXPENSE": ("626151", AccountType.EXPENSE, NormalBalance.DEBIT),
    "INSURANCE_EXPENSE": ("626800", AccountType.EXPENSE, NormalBalance.DEBIT),
    "PROFESSIONAL_FEES_EXPENSE": ("626530", AccountType.EXPENSE, NormalBalance.DEBIT),
    "WATER_EXPENSE": ("626330", AccountType.EXPENSE, NormalBalance.DEBIT),
    "FREIGHT_OUT_EXPENSE": ("626111", AccountType.EXPENSE, NormalBalance.DEBIT),
    "MUNICIPALITY_TAX_EXPENSE": ("642000", AccountType.EXPENSE, NormalBalance.DEBIT),
    "REGISTRATION_FEES_EXPENSE": ("644000", AccountType.EXPENSE, NormalBalance.DEBIT),
    "TAX_PENALTIES_EXPENSE": ("645801", AccountType.EXPENSE, NormalBalance.DEBIT),
    "VAT_NONRECOVERABLE_EXPENSE": ("643000", AccountType.EXPENSE, NormalBalance.DEBIT),
    "MEDICAL_EXPENSE": ("626910", AccountType.EXPENSE, NormalBalance.DEBIT),
    "DONATIONS_EXPENSE": ("685110", AccountType.EXPENSE, NormalBalance.DEBIT),
    "INTEREST_EXPENSE": ("673100", AccountType.EXPENSE, NormalBalance.DEBIT),
    "CASH_PETTY": ("530002", AccountType.ASSET, NormalBalance.DEBIT),
    "SALES_DISCOUNTS": ("709000", AccountType.INCOME, NormalBalance.DEBIT),
    "CREDIT_CARD_CLEARING": ("540005", AccountType.ASSET, NormalBalance.DEBIT),
    "CAPITAL": ("101301", AccountType.EQUITY, NormalBalance.CREDIT),
    "LEGAL_RESERVE": ("111001", AccountType.EQUITY, NormalBalance.CREDIT),
    "DEPOSITS_PAID": ("259001", AccountType.ASSET, NormalBalance.DEBIT),
    "PROFIT_BROUGHT_FORWARD": ("121001", AccountType.EQUITY, NormalBalance.CREDIT),
    "LOSS_BROUGHT_FORWARD": ("125001", AccountType.EQUITY, NormalBalance.DEBIT),
    "FA_OFFICE_EQUIPMENT": ("226211", AccountType.ASSET, NormalBalance.DEBIT),
    "FA_COMPUTER": ("226221", AccountType.ASSET, NormalBalance.DEBIT),
    "FA_FURNITURE": ("226311", AccountType.ASSET, NormalBalance.DEBIT),
    "FA_INSTALLATIONS": ("226101", AccountType.ASSET, NormalBalance.DEBIT),
    "FA_VEHICLES": ("225101", AccountType.ASSET, NormalBalance.DEBIT),
    "FA_ACCUM_DEP_OFFICE": ("282621", AccountType.ASSET, NormalBalance.CREDIT),
    "FA_ACCUM_DEP_COMPUTER": ("282622", AccountType.ASSET, NormalBalance.CREDIT),
    "FA_ACCUM_DEP_FURNITURE": ("282631", AccountType.ASSET, NormalBalance.CREDIT),
    "FA_ACCUM_DEP_INSTALLATIONS": ("282611", AccountType.ASSET, NormalBalance.CREDIT),
    "FA_ACCUM_DEP_VEHICLES": ("282521", AccountType.ASSET, NormalBalance.CREDIT),
    "DEP_EXPENSE_OFFICE": ("651262", AccountType.EXPENSE, NormalBalance.DEBIT),
    "DEP_EXPENSE_FURNITURE": ("651263", AccountType.EXPENSE, NormalBalance.DEBIT),
    "DEP_EXPENSE_INSTALLATIONS": ("651261", AccountType.EXPENSE, NormalBalance.DEBIT),
    "DEP_EXPENSE_VEHICLES": ("651251", AccountType.EXPENSE, NormalBalance.DEBIT),
    "FA_DISPOSAL_GAIN": ("781200", AccountType.INCOME, NormalBalance.CREDIT),
    "FA_DISPOSAL_NBV": ("681200", AccountType.EXPENSE, NormalBalance.DEBIT),
}


@pytest.mark.asyncio
async def test_new_accounts_seeded(db):
    await seed_chart_of_accounts(db)
    rows = (await db.execute(select(GLAccount))).scalars().all()
    by_key = {a.system_key: a for a in rows}
    for key, (code, atype, nb) in NEW_ACCOUNTS.items():
        a = by_key.get(key)
        assert a is not None, f"{key} not seeded"
        assert a.code == code and a.type == atype and a.normal_balance == nb, f"{key} mismatch"


def test_result_of_period_not_seeded():
    keys = {t[6] for t in SYSTEM_ACCOUNTS}
    assert "RESULT_PERIOD_PROFIT" not in keys and "RESULT_PERIOD_LOSS" not in keys


def test_migration_map_matches_seed():
    # The migration's RENUMBER must cover every renumbered account and be 1:1.
    mod = _load_renumber_migration()
    MIG, OLD = mod.RENUMBER, mod.OLD
    assert set(MIG) == set(RENUMBER), "migration RENUMBER must cover the renumber map"
    assert set(OLD) == set(RENUMBER), "migration OLD (downgrade) must cover the renumber map"
    assert MIG == RENUMBER, "migration RENUMBER must match the test/seed renumber map"
    assert len(set(MIG.values())) == len(MIG), "new codes must be unique"
    assert len(set(OLD.values())) == len(OLD), "old codes must be unique"
