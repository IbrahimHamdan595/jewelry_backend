"""NEX-54 — money leaves the API as an exact decimal string, never a float."""
import json
from decimal import Decimal as D

import pytest
from pydantic import BaseModel

from app.core.money import Money, money, round_money


def test_money_is_an_exact_two_decimal_string():
    assert money(D("1234.5")) == "1234.50"
    assert money(D("1234.56")) == "1234.56"
    assert money(D("0")) == "0.00"
    assert money(0) == "0.00"                       # COALESCE(SUM(...), 0) on an empty set
    assert money(D("-10.1")) == "-10.10"
    assert money(D("0.1") + D("0.2")) == "0.30"     # the classic float artefact cannot happen
    # A full NUMERIC(18,2) value: as a JSON number it would lose its cents.
    assert money(D("1234567890123456.78")) == "1234567890123456.78"
    assert json.dumps(float(D("1234567890123456.78"))) == "1234567890123456.8"


# Half-cent ties, with the answer the pricing engine and Postgres NUMERIC give.
_TIES = [("2.345", "2.35"), ("2.355", "2.36"), ("63.225", "63.23"), ("70.105", "70.11"),
         ("0.005", "0.01"), ("126.465", "126.47"), ("-2.345", "-2.35"), ("-0.005", "-0.01"),
         ("1.0049999", "1.00"), ("1.0050001", "1.01")]


@pytest.mark.parametrize("amount, expected", _TIES)
def test_money_rounds_half_up_like_pricing_and_postgres(amount, expected):
    """One rounding rule for every endpoint: the same amount cannot be a cent
    apart depending on which code path rendered it."""
    from app.core.pricing import _round

    assert money(D(amount)) == expected
    assert round_money(D(amount)) == D(expected)
    assert money(D(amount)) == str(_round(D(amount)))       # pricing._round is ROUND_HALF_UP


def test_money_matches_the_price_the_pricing_engine_quotes():
    # 84.30/g at 18K: 63.225. Half-even said 63.22 while the lookup said 63.23.
    from app.core.pricing import KARAT_PURITY, calculate_price
    from app.models import Karat

    priced = calculate_price(rate_24k=D("84.30"), karat=Karat.K18, weight_grams=D("1"),
                             margin_percent=D("0"), making_charge=D("0"))
    assert money(D("84.30") * KARAT_PURITY[Karat.K18]) == str(priced["purity_rate"]) == "63.23"


def test_money_follows_the_column_scale_when_told():
    assert money(D("2"), quantum=D("0.0001")) == "2.0000"   # e.g. markup_per_gram NUMERIC(10,4)
    assert money(D("89500"), quantum=D("0.000001")) == "89500.000000"
    assert money(D("0.00005"), quantum=D("0.0001")) == "0.0001"


def test_quantum_is_keyword_only_and_the_field_serializer_takes_one_argument():
    """pydantic decides whether to hand a serializer its SerializationInfo by
    counting positional parameters, and the count rule differs across 2.x. A
    second positional parameter on the function it calls could receive that
    object as the quantum; make it impossible."""
    import inspect
    import typing

    from pydantic import PlainSerializer

    assert inspect.signature(money).parameters["quantum"].kind is inspect.Parameter.KEYWORD_ONLY
    with pytest.raises(TypeError):
        money(D("1"), D("0.0001"))

    (serializer,) = [m for m in typing.get_args(Money)[1:] if isinstance(m, PlainSerializer)]
    params = list(inspect.signature(serializer.func).parameters.values())
    assert len(params) == 1 and serializer.func is not money
    assert serializer.func(D("5")) == "5.00"


def test_money_never_emits_exponents_or_negative_zero():
    assert money(D("1E+3")) == "1000.00"
    assert money(D("0E-7")) == "0.00"
    assert money(D("-0.001")) == "0.00"
    assert money(D("-0.004")) == "0.00"


def test_money_refuses_floats():
    with pytest.raises(TypeError):
        money(0.1)
    with pytest.raises(TypeError):
        money(None)


class _Invoice(BaseModel):
    total: Money
    discount: Money | None = None


def test_money_field_serialises_as_a_string_and_stays_a_decimal_in_python():
    inv = _Invoice(total=D("1234.5"))
    assert inv.total == D("1234.5") and isinstance(inv.total, D)
    assert inv.model_dump_json() == '{"total":"1234.50","discount":null}'
    assert json.loads(_Invoice(total=D("5"), discount=D("0.1")).model_dump_json()) == {
        "total": "5.00", "discount": "0.10"}
    assert _Invoice.model_json_schema(mode="serialization")["properties"]["total"]["type"] == "string"
