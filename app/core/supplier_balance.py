"""Supplier balance mutation helper.

Per-(supplier, unit, karat) running balances. Gold balances stay in grams
forever, with karat. Cash balances have karat="". A moving gold rate never
silently revalues debt.
"""

from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DebtUnit, SupplierBalance


async def adjust_balance(
    db: AsyncSession,
    *,
    supplier_id: str,
    unit: DebtUnit,
    karat: str,  # "" for CASH
    delta: Decimal,
    allow_negative: bool = False,
) -> tuple[Decimal, Decimal]:
    """Add `delta` to the (supplier, unit, karat) running balance.

    Positive delta = owed grows (purchase on credit).
    Negative delta = owed shrinks (repayment).

    Returns (balance_before, balance_after). Locks the row FOR UPDATE.
    Raises 422 if the result would push balance below zero (overpayment).
    """
    row = (
        await db.execute(
            select(SupplierBalance)
            .where(
                SupplierBalance.supplier_id == supplier_id,
                SupplierBalance.unit == unit,
                SupplierBalance.karat == karat,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()

    if row is None:
        if delta < 0 and not allow_negative:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Cannot repay {abs(delta)} of {unit.value}"
                    + (f" K{karat}" if karat else "")
                    + " — no outstanding balance"
                ),
            )
        row = SupplierBalance(
            supplier_id=supplier_id,
            unit=unit,
            karat=karat,
            balance=Decimal("0"),
        )
        db.add(row)
        await db.flush()

    before = row.balance
    after = before + delta
    if after < 0 and not allow_negative:
        owed_label = f"{unit.value}" + (f" K{karat}" if karat else "")
        raise HTTPException(
            status_code=422,
            detail=(
                f"Repayment of {abs(delta)} {owed_label} exceeds outstanding balance "
                f"{before}. Refusing to overpay."
            ),
        )

    row.balance = after
    await db.flush()
    return before, after


async def get_supplier_balances(db: AsyncSession, supplier_id: str) -> list[SupplierBalance]:
    rows = (
        await db.execute(
            select(SupplierBalance)
            .where(SupplierBalance.supplier_id == supplier_id)
        )
    ).scalars().all()
    # Hide zero balances from callers — they're just historical artifacts.
    return [r for r in rows if r.balance != 0]
