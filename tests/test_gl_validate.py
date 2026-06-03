from decimal import Decimal

from app.core.gl import GLLine, validate_balanced

D = Decimal


def money(account_id, denom, debit="0", credit="0"):
    return GLLine(account_id=account_id, denomination=denom,
                  base_debit=D(debit), base_credit=D(credit),
                  money_debit=D(debit), money_credit=D(credit))


def test_balanced_money_only_ok():
    lines = [money("cash", "MONEY", debit="100"), money("rev", "MONEY", credit="100")]
    assert validate_balanced(lines) == []


def test_unbalanced_money_rejected():
    lines = [money("cash", "MONEY", debit="100"), money("rev", "MONEY", credit="90")]
    errs = validate_balanced(lines)
    assert any("money" in e.lower() for e in errs)


def test_empty_rejected():
    assert validate_balanced([]) == ["at least one line is required"]


def test_dual_sale_balances_money_and_metal_per_karat():
    # Sale: money side cash 100 / revenue 100 ; metal side COGS grams in / inventory grams out (K21)
    lines = [
        money("cash", "MONEY", debit="100"),
        money("revenue", "MONEY", credit="100"),
        GLLine(account_id="cogs", denomination="DUAL", base_debit=D("60"),
               metal_debit_grams=D("10.000"), karat="K21"),
        GLLine(account_id="inventory", denomination="DUAL", base_credit=D("60"),
               metal_credit_grams=D("10.000"), karat="K21"),
    ]
    assert validate_balanced(lines) == []


def test_metal_unbalanced_per_karat_rejected():
    lines = [
        GLLine(account_id="cogs", denomination="DUAL", base_debit=D("60"),
               metal_debit_grams=D("10.000"), karat="K21"),
        GLLine(account_id="inventory", denomination="DUAL", base_credit=D("60"),
               metal_credit_grams=D("9.000"), karat="K21"),
    ]
    errs = validate_balanced(lines)
    assert any("K21" in e for e in errs)


def test_money_account_cannot_carry_metal():
    lines = [
        GLLine(account_id="cash", denomination="MONEY", base_debit=D("60"),
               metal_debit_grams=D("10.000"), karat="K21"),
        GLLine(account_id="inv", denomination="DUAL", base_credit=D("60"),
               metal_credit_grams=D("10.000"), karat="K21"),
    ]
    errs = validate_balanced(lines)
    assert any("MONEY account" in e for e in errs)


def test_metal_line_requires_karat():
    lines = [
        GLLine(account_id="cogs", denomination="DUAL", base_debit=D("60"),
               metal_debit_grams=D("10.000"), karat=None),
        GLLine(account_id="inv", denomination="DUAL", base_credit=D("60"),
               metal_credit_grams=D("10.000"), karat=None),
    ]
    errs = validate_balanced(lines)
    assert any("karat" in e.lower() for e in errs)
