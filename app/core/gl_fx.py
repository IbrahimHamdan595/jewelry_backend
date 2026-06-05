"""Realized FX on AR/AP settlement (Odoo parity) — the pure leg builder.

A settlement (AR receipt or AP vendor payment) is **recorded in** the document's
currency (`recorded_currency` == the invoice/bill currency — this is the axis we
reject cross-currency on). The physical cash leg lands in a **tender** account
whose own currency may differ (e.g. a USD invoice paid with LBP cash → CASH_LBP
at today's rate). The control account is relieved at each allocated document's
*captured* booking rate, and the realized FX difference is plugged to FX_LOSS
(debit) / FX_GAIN (credit) so the base (USD) dimension stays balanced.

Pure: no DB, no clock. `rate` is LBP per 1 USD; `base = money / rate`.

Deferred Odoo divergences (NOT defects — see Phase-5 candidates):
  • Cross-currency *settlement* (paying a foreign doc in a different currency and
    auto-converting between the two) is rejected here in v1.
  • Unrealized period-end FX revaluation of open foreign balances is not done;
    only realized FX at settlement.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")
_Q_MONEY = Decimal("0.01")

FX_LOSS = "FX_LOSS"
FX_GAIN = "FX_GAIN"


@dataclass
class Leg:
    """A resolved settlement leg. The caller maps system_key → account id and
    builds a gl.GLLine (debit → base_debit/money_debit, else the credit fields)."""
    system_key: str
    debit: bool
    money: Decimal
    base: Decimal
    currency: str
    fx_rate: Decimal
    memo: str


@dataclass
class Allocation:
    invoice_currency: str
    invoice_fx_rate: Decimal   # captured at booking, LBP per USD
    applied_money: Decimal     # in recorded_currency (== invoice_currency)


def settlement_legs(
    *,
    kind: str,                     # "receipt" (AR) | "payment" (AP)
    recorded_currency: str,        # currency the settlement is booked in (= doc currency)
    allocations: list[Allocation],
    control_system_key: str,       # "AR" | "VENDOR_AP"
    cash_system_key: str,          # the tender account
    tender_currency: str,          # the tender account's currency
    tender_fx_rate: Decimal,       # LBP per USD for the tender currency today (1 if USD)
) -> list[Leg]:
    if kind not in ("receipt", "payment"):
        raise ValueError(f"unknown settlement kind {kind!r}")
    if not allocations:
        raise ValueError("settlement requires at least one allocation")

    # Reject cross-currency settlement: the doc must be paid in its own currency.
    for al in allocations:
        if al.invoice_currency != recorded_currency:
            raise ValueError(
                f"settlement recorded in {recorded_currency} cannot be applied to a "
                f"{al.invoice_currency} document — record it in {al.invoice_currency}")

    tender_fx_rate = Decimal(tender_fx_rate)
    control_is_debit = (kind == "payment")   # AP relief debits the control; AR relief credits it
    cash_is_debit = (kind == "receipt")       # cash in (receipt) debits; cash out (payment) credits

    legs: list[Leg] = []

    # Control legs — one per allocation, each relieved at its captured rate so
    # base = money / fx_rate stays exact per line.
    total_recorded_money = ZERO
    for al in allocations:
        money = Decimal(al.applied_money).quantize(_Q_MONEY)
        base = (money / Decimal(al.invoice_fx_rate)).quantize(_Q_MONEY)
        total_recorded_money += money
        legs.append(Leg(control_system_key, control_is_debit, money, base,
                        recorded_currency, Decimal(al.invoice_fx_rate),
                        "relieve AR" if kind == "receipt" else "relieve AP"))

    # Cash leg — valued in the tender account's currency at the tender rate.
    if tender_currency == recorded_currency:
        cash_money = total_recorded_money
    elif recorded_currency == "USD":
        # USD doc settled with foreign cash (e.g. USD invoice paid LBP): the
        # cash is the USD value × today's rate; base stays the USD value.
        cash_money = (total_recorded_money * tender_fx_rate).quantize(_Q_MONEY)
    else:
        raise ValueError(
            f"cannot settle a {recorded_currency} document with {tender_currency} cash; "
            f"use a {recorded_currency} cash account")
    cash_money = cash_money.quantize(_Q_MONEY)
    cash_base = (cash_money / tender_fx_rate).quantize(_Q_MONEY) if tender_fx_rate != 1 else cash_money
    legs.append(Leg(cash_system_key, cash_is_debit, cash_money, cash_base,
                    tender_currency, tender_fx_rate,
                    "receipt" if kind == "receipt" else "cash out"))

    # FX plug — whatever base makes the entry balance; the side picks the account.
    credit_base = sum((l.base for l in legs if not l.debit), ZERO)
    debit_base = sum((l.base for l in legs if l.debit), ZERO)
    plug = (credit_base - debit_base).quantize(_Q_MONEY)   # >0 ⇒ need a debit ⇒ FX_LOSS
    if plug > ZERO:
        legs.append(Leg(FX_LOSS, True, plug, plug, "USD", Decimal("1"), "realized FX loss"))
    elif plug < ZERO:
        legs.append(Leg(FX_GAIN, False, -plug, -plug, "USD", Decimal("1"), "realized FX gain"))

    return legs
