"""NEX-54 — money leaves the API as an exact decimal string, never a float."""
import json
from decimal import Decimal as D

import pytest
from pydantic import BaseModel

from app.core.money import Money, money


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


def test_money_quantizes_like_the_rest_of_the_codebase():
    # Decimal.quantize default (half-even), the same call as gl._q_money.
    assert money(D("411.6200000000000000000000000")) == "411.62"
    assert money(D("2.345")) == "2.34"
    assert money(D("2.355")) == "2.36"


def test_money_follows_the_column_scale_when_told():
    assert money(D("2"), D("0.0001")) == "2.0000"           # e.g. markup_per_gram NUMERIC(10,4)
    assert money(D("89500"), D("0.000001")) == "89500.000000"


def test_money_never_emits_exponents_or_negative_zero():
    assert money(D("1E+3")) == "1000.00"
    assert money(D("0E-7")) == "0.00"
    assert money(D("-0.001")) == "0.00"


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
