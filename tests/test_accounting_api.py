import pytest

from app.models import Role, AccountType, Denomination, NormalBalance, PeriodStatus


def test_role_enum_has_accounting_roles():
    assert Role.ACCOUNTANT.value == "ACCOUNTANT"
    assert Role.MANAGER.value == "MANAGER"
    # Existing roles unchanged
    assert Role.ADMIN.value == "ADMIN"
    assert Role.CASHIER.value == "CASHIER"


def test_gl_enums_present():
    assert {t.value for t in AccountType} == {
        "ASSET", "LIABILITY", "EQUITY", "INCOME", "EXPENSE"
    }
    assert {d.value for d in Denomination} == {"MONEY", "METAL", "DUAL"}
    assert {n.value for n in NormalBalance} == {"DEBIT", "CREDIT"}
    assert {p.value for p in PeriodStatus} == {"OPEN", "CLOSED"}
