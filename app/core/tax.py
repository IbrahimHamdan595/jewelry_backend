"""Tax / VAT (Module 6): tax codes + the Lebanon quarterly VAT return.

Output VAT (sales) is posted to VAT_PAYABLE by M1; input VAT (vendor bills) is
posted to VAT_RECEIVABLE by expenses.post_vendor_bill. The return nets the two
over a quarter."""
from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GLAccount, GLJournalEntry, GLJournalLine, TaxCode

ZERO = Decimal("0")
_Q_MONEY = Decimal("0.01")

# (code, name, rate)
_SEED_CODES = [("STANDARD", "Standard 11%", Decimal("11.00")),
               ("ZERO", "Zero-rated (exports)", Decimal("0.00")),
               ("EXEMPT", "Exempt", Decimal("0.00"))]


async def seed_tax_codes(db: AsyncSession) -> int:
    existing = {c for (c,) in (await db.execute(select(TaxCode.code))).all()}
    created = 0
    for code, name, rate in _SEED_CODES:
        if code in existing:
            continue
        db.add(TaxCode(code=code, name=name, rate=rate))
        created += 1
    await db.flush()
    return created


def _quarter_range(year: int, quarter: int) -> tuple[date, date]:
    if quarter not in (1, 2, 3, 4):
        from fastapi import HTTPException
        raise HTTPException(422, "quarter must be 1–4")
    start_month = (quarter - 1) * 3 + 1
    end_month = quarter * 3
    last_day = calendar.monthrange(year, end_month)[1]
    return date(year, start_month, 1), date(year, end_month, last_day)


async def _account_id(db: AsyncSession, system_key: str) -> str:
    return (await db.execute(select(GLAccount).where(GLAccount.system_key == system_key))).scalar_one().id


async def compute_vat_return(db: AsyncSession, *, year: int, quarter: int) -> dict:
    frm, until = _quarter_range(year, quarter)
    out_id = await _account_id(db, "VAT_PAYABLE")
    in_id = await _account_id(db, "VAT_RECEIVABLE")
    rows = (await db.execute(
        select(GLJournalLine, GLJournalEntry)
        .join(GLJournalEntry, GLJournalLine.entry_id == GLJournalEntry.id)
        .where(GLJournalEntry.entry_date >= frm, GLJournalEntry.entry_date <= until,
               GLJournalLine.account_id.in_([out_id, in_id]))
    )).all()
    output_vat = ZERO
    input_vat = ZERO
    txns = []
    for line, entry in rows:
        if line.account_id == out_id:
            amt = line.base_credit - line.base_debit
            output_vat += amt
            kind = "output"
        else:
            amt = line.base_debit - line.base_credit
            input_vat += amt
            kind = "input"
        txns.append({"entry_no": entry.entry_no, "date": entry.entry_date,
                     "source_type": entry.source_type, "kind": kind, "vat": amt.quantize(_Q_MONEY)})
    output_vat = output_vat.quantize(_Q_MONEY)
    input_vat = input_vat.quantize(_Q_MONEY)
    net = (output_vat - input_vat).quantize(_Q_MONEY)
    direction = "PAYABLE" if net > 0 else ("REFUNDABLE" if net < 0 else "NIL")
    cash_split = None
    if net > 0:
        cash_split = {"cash_75": (net * Decimal("0.75")).quantize(_Q_MONEY),
                      "transfer_25": (net * Decimal("0.25")).quantize(_Q_MONEY),
                      "bdl_account": "700361115",
                      "note": "Crisis-era 75/25 cash split — verify current MoF instructions; not enforced in GL."}
    txns.sort(key=lambda t: (t["date"], t["entry_no"]))
    return {"year": year, "quarter": quarter, "from": frm, "until": until,
            "output_vat": output_vat, "input_vat": input_vat, "net_payable": net,
            "direction": direction, "transactions": txns, "cash_split": cash_split}
