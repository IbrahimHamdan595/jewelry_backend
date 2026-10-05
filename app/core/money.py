"""Exact money at the API boundary (NEX-54).

Money is stored and computed as Decimal. It must leave the API the same way:
as a decimal string ("1234.56"), never a JSON number — a float cannot hold
most cent values exactly, and every JSON client parses numbers as floats.
The client formats; the server never rounds through binary.

`money()` is the one place that turns a Decimal into that string, so the
quantisation is the same everywhere: cents by default, or the scale of the
column the value comes from (pass `quantum`, e.g. 0.0001 for a NUMERIC(…, 4)
rate). Pydantic response models declare the field as `Money` instead, which
serialises through the same function.
"""
from decimal import Decimal
from typing import Annotated

from pydantic import PlainSerializer

CENTS = Decimal("0.01")


def money(value: Decimal | int, quantum: Decimal = CENTS) -> str:
    """Render a monetary amount as an exact decimal string at `quantum` scale.

    Floats are refused rather than converted: a float here means the amount has
    already been through binary rounding somewhere upstream."""
    if isinstance(value, float) or not isinstance(value, (Decimal, int)):
        raise TypeError(f"money() needs a Decimal, got {type(value).__name__}: {value!r}")
    q = Decimal(value).quantize(quantum)
    return str(abs(q) if q.is_zero() else q)       # never "-0.00"


# A response-model field holding money: a Decimal in Python, an exact
# two-decimal string in JSON (plain Decimal fields serialise at whatever scale
# the value happens to carry).
Money = Annotated[Decimal, PlainSerializer(money, return_type=str, when_used="json")]
