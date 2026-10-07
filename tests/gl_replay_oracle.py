"""Line-by-line trial balance — the test oracle for the SQL aggregation.

`compute_trial_balance_replay` is `app.core.gl.compute_trial_balance` exactly as
it stood before NEX-53 moved the sums into SQL: select every journal line whose
entry_date <= as_of and accumulate in Python. tests/test_gl_trial_balance.py
asserts the SQL result equals this one. The body is unchanged — do not "tidy"
it; its value is that it does not move with the code under test.
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.gl import ZERO, _q_grams, _q_money
from app.models import GLAccount, GLJournalEntry, GLJournalLine


async def compute_trial_balance_replay(db: AsyncSession, *, as_of: date) -> dict:
    rows = (
        await db.execute(
            select(GLJournalLine, GLJournalEntry, GLAccount)
            .join(GLJournalEntry, GLJournalLine.entry_id == GLJournalEntry.id)
            .join(GLAccount, GLJournalLine.account_id == GLAccount.id)
            .where(GLJournalEntry.entry_date <= as_of)
        )
    ).all()

    accounts: dict[str, dict] = {}
    total_d = ZERO
    total_c = ZERO
    metal_totals: dict[str, dict[str, Decimal]] = {}

    for line, entry, acct in rows:
        a = accounts.setdefault(acct.id, {
            "account_id": acct.id, "code": acct.code, "name": acct.name,
            "type": acct.type.value, "system_key": acct.system_key,
            "base_debit": ZERO, "base_credit": ZERO,
            "money_by_currency": {}, "metal_by_karat": {},
        })
        a["base_debit"] += line.base_debit
        a["base_credit"] += line.base_credit
        total_d += line.base_debit
        total_c += line.base_credit

        cur = a["money_by_currency"].setdefault(line.currency, {"debit": ZERO, "credit": ZERO})
        cur["debit"] += line.money_debit
        cur["credit"] += line.money_credit

        if line.metal_debit_grams or line.metal_credit_grams:
            k = line.karat or "?"
            mk = a["metal_by_karat"].setdefault(k, {"debit_grams": ZERO, "credit_grams": ZERO})
            mk["debit_grams"] += line.metal_debit_grams
            mk["credit_grams"] += line.metal_credit_grams
            mt = metal_totals.setdefault(k, {"debit_grams": ZERO, "credit_grams": ZERO})
            mt["debit_grams"] += line.metal_debit_grams
            mt["credit_grams"] += line.metal_credit_grams

    out_accounts = []
    for a in sorted(accounts.values(), key=lambda x: x["code"]):
        a["base_debit"] = _q_money(a["base_debit"])
        a["base_credit"] = _q_money(a["base_credit"])
        a["net_base"] = _q_money(a["base_debit"] - a["base_credit"])
        a["money_by_currency"] = {
            c: {"debit": _q_money(v["debit"]), "credit": _q_money(v["credit"])}
            for c, v in a["money_by_currency"].items()
        }
        a["metal_by_karat"] = {
            k: {
                "debit_grams": _q_grams(v["debit_grams"]),
                "credit_grams": _q_grams(v["credit_grams"]),
                "net_grams": _q_grams(v["debit_grams"] - v["credit_grams"]),
            }
            for k, v in a["metal_by_karat"].items()
        }
        out_accounts.append(a)

    metal_by_karat = {
        k: {"debit_grams": _q_grams(v["debit_grams"]), "credit_grams": _q_grams(v["credit_grams"])}
        for k, v in sorted(metal_totals.items())
    }

    return {
        "as_of": as_of,
        "accounts": out_accounts,
        "total_base_debit": _q_money(total_d),
        "total_base_credit": _q_money(total_c),
        "balanced": _q_money(total_d) == _q_money(total_c),
        "metal_by_karat": metal_by_karat,
        "metal_balanced": all(v["debit_grams"] == v["credit_grams"] for v in metal_by_karat.values()),
    }
