"""System chart-of-accounts definition + idempotent seeder + opening balances.

system_key accounts are resolved by key by the M1 auto-posting bridge; they
can be deactivated but never deleted (design §3.5).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import gl
from app.core.pricing import KARAT_PURITY
from app.models import (
    AccountType, CoinType, DebtUnit, Denomination, GLAccount, GoldLot, NormalBalance,
    OunceType, Product, ProductStatus, SupplierBalance,
)

A, L, EQ, INC, EXP = (
    AccountType.ASSET, AccountType.LIABILITY, AccountType.EQUITY,
    AccountType.INCOME, AccountType.EXPENSE,
)
M, MT, DU = Denomination.MONEY, Denomination.METAL, Denomination.DUAL
DR, CR = NormalBalance.DEBIT, NormalBalance.CREDIT

# (code, name, type, denomination, normal_balance, currency, system_key)
SYSTEM_ACCOUNTS: list[tuple] = [
    ("1000", "Cash (USD)",            A,  M,  DR, "USD", "CASH"),
    ("1010", "Cash (LBP)",            A,  M,  DR, "LBP", "CASH_LBP"),
    ("1020", "Bank",                  A,  M,  DR, "USD", "BANK"),
    ("1100", "Accounts Receivable",   A,  M,  DR, "USD", "AR"),
    ("1200", "Metal Inventory",       A,  DU, DR, "USD", "METAL_INVENTORY"),
    ("1210", "Product Inventory",     A,  M,  DR, "USD", "PRODUCT_INVENTORY"),
    ("1300", "VAT Receivable (input)",A,  M,  DR, "USD", "VAT_RECEIVABLE"),
    ("2000", "Accounts Payable",      L,  M,  CR, "USD", "AP"),
    ("2100", "Metal AP",              L,  DU, CR, "USD", "METAL_AP"),
    ("2200", "VAT Payable (output)",  L,  M,  CR, "USD", "VAT_PAYABLE"),
    ("2300", "Customer Deposits",     L,  M,  CR, "USD", "CUSTOMER_DEPOSITS"),
    # DUAL: opening equity carries the money plug AND the per-karat metal
    # counterpart for physical gold contributed as opening capital (the gold on
    # hand not financed by supplier metal debt). See post_opening_balances.
    ("3000", "Opening Balance Equity",EQ, DU, CR, "USD", "OPENING_BALANCE_EQUITY"),
    ("3100", "Retained Earnings",     EQ, M,  CR, "USD", "RETAINED_EARNINGS"),
    ("4000", "Sales Revenue",         INC, M, CR, "USD", "SALES_REVENUE"),
    ("4100", "Making-Charge Revenue", INC, M, CR, "USD", "MAKING_CHARGE_REVENUE"),
    # Realized FX split (Odoo parity): gain → INCOME (credit), loss → EXPENSE
    # (debit). Reported together as "Other income/(expense)" below operating.
    ("4900", "FX Gain",               INC, M, CR, "USD", "FX_GAIN"),
    ("5000", "Metal COGS",            EXP, DU, DR, "USD", "METAL_COGS"),
    ("5100", "Making COGS",           EXP, M,  DR, "USD", "MAKING_COGS"),
    ("6900", "FX Loss",               EXP, M,  DR, "USD", "FX_LOSS"),
    # Module 1 (auto-posting) additions:
    ("1250", "Metal Clearing",        A,  DU, DR, "USD", "METAL_CLEARING"),
    ("5200", "Inventory Adjustment Expense", EXP, M, DR, "USD", "ADJUSTMENT_EXPENSE"),
    # Module 5 (Expenses & Purchasing):
    ("2400", "Vendor Payables",       L,  M,  CR, "USD", "VENDOR_AP"),
    ("6000", "Rent Expense",          EXP, M, DR, "USD", "RENT_EXPENSE"),
    ("6100", "Utilities Expense",     EXP, M, DR, "USD", "UTILITIES_EXPENSE"),
    ("6200", "Salaries Expense",      EXP, M, DR, "USD", "SALARIES_EXPENSE"),
    ("6300", "Marketing Expense",     EXP, M, DR, "USD", "MARKETING_EXPENSE"),
    ("6400", "Bank Charges",          EXP, M, DR, "USD", "BANK_CHARGES_EXPENSE"),
    ("6500", "Office Supplies",       EXP, M, DR, "USD", "OFFICE_EXPENSE"),
    ("6800", "Miscellaneous Expense", EXP, M, DR, "USD", "MISC_EXPENSE"),
]


async def seed_chart_of_accounts(db: AsyncSession) -> int:
    """Insert any missing system accounts. Returns the number created.
    Idempotent — keyed on system_key. Does NOT commit."""
    existing = {
        k for (k,) in (await db.execute(select(GLAccount.system_key))).all() if k
    }
    created = 0
    for code, name, type_, denom, normal, currency, key in SYSTEM_ACCOUNTS:
        if key in existing:
            continue
        db.add(GLAccount(
            code=code, name=name, type=type_, denomination=denom,
            normal_balance=normal, currency=(None if denom is MT else currency),
            system_key=key, is_active=True,
        ))
        created += 1
    await db.flush()
    return created


async def _key_to_account_id(db: AsyncSession, system_key: str) -> str:
    acct = (
        await db.execute(select(GLAccount).where(GLAccount.system_key == system_key))
    ).scalar_one()
    return acct.id


async def post_opening_balances(
    db: AsyncSession,
    *,
    as_of: date,
    actor_user_id: str,
    cash_lines: list[dict] | None = None,
    gold_rate_24k: Decimal | None = None,
):
    """One-time OPENING entry seeded from current state, balanced against
    OPENING_BALANCE_EQUITY (design §3.5):
      • Metal Inventory: undepleted gold lots → grams (per karat) + cost (DR).
      • Metal AP: supplier GOLD balances → grams per karat (CR liability).
      • AP: supplier CASH balances → USD (CR liability).
      • Cash/Bank: manually-entered `cash_lines` ([{system_key, amount}]) (DR).
    OPENING_BALANCE_EQUITY (DUAL) absorbs the net in BOTH dimensions:
      • money: net of asset DRs minus liability CRs;
      • metal per karat: net grams = inventory on hand − supplier-owed grams
        (owner's contributed metal). A negative net (owe more than on hand)
        becomes an equity metal DEBIT, which still balances the karat.
    """
    cash_lines = cash_lines or []
    lines: list[gl.GLLine] = []

    inv_id = await _key_to_account_id(db, "METAL_INVENTORY")
    metal_ap_id = await _key_to_account_id(db, "METAL_AP")
    ap_id = await _key_to_account_id(db, "AP")
    equity_id = await _key_to_account_id(db, "OPENING_BALANCE_EQUITY")

    equity_credit = Decimal("0")            # net money the equity plug must CR
    metal_net_by_karat: dict[str, Decimal] = {}  # grams the equity plug must CR (per karat)

    # 1. Metal inventory from gold lots (money cost + grams), DR inventory.
    lots = (
        await db.execute(select(GoldLot).where(GoldLot.is_depleted.is_(False)))
    ).scalars().all()
    for lot in lots:
        kar = lot.karat.value
        lines.append(gl.GLLine(
            account_id=inv_id, denomination="DUAL",
            base_debit=lot.cost_basis_usd, metal_debit_grams=lot.weight_remaining_grams,
            karat=kar, memo=f"opening lot {lot.id}",
        ))
        equity_credit += lot.cost_basis_usd
        metal_net_by_karat[kar] = metal_net_by_karat.get(kar, Decimal("0")) + lot.weight_remaining_grams

    # 1b. Finished-goods metal (products/coins/ounces) → Metal Inventory, DR.
    # Without this, selling finished goods drives METAL_INVENTORY negative since
    # only raw lots were loaded above (M1 concern #1). Cost: tracked cost when
    # present, else a gold-rate proxy (gold_rate_24k × pure grams).
    rate = Decimal(str(gold_rate_24k)) if gold_rate_24k is not None else Decimal("0")

    def _fg_cost(weight, karat, qty, tracked):
        if tracked is not None:
            return (tracked * qty).quantize(Decimal("0.01"))
        return (rate * weight * KARAT_PURITY[karat] * qty).quantize(Decimal("0.01"))

    products = (await db.execute(
        select(Product).where(Product.status.in_((ProductStatus.AVAILABLE, ProductStatus.RESERVED)),
                              Product.on_hand_qty > 0)
    )).scalars().all()
    for p in products:
        kar = p.karat.value
        grams = p.weight_grams * p.on_hand_qty
        cost = _fg_cost(p.weight_grams, p.karat, p.on_hand_qty, p.cost_basis_usd)
        lines.append(gl.GLLine(account_id=inv_id, denomination="DUAL", base_debit=cost,
                               metal_debit_grams=grams, karat=kar, memo=f"opening product {p.code}"))
        equity_credit += cost
        metal_net_by_karat[kar] = metal_net_by_karat.get(kar, Decimal("0")) + grams

    for model, label in ((CoinType, "coin"), (OunceType, "ounce")):
        rows = (await db.execute(select(model).where(model.on_hand_qty > 0))).scalars().all()
        for r in rows:
            kar = r.karat.value
            grams = r.weight_grams * r.on_hand_qty
            cost = _fg_cost(r.weight_grams, r.karat, r.on_hand_qty, None)
            lines.append(gl.GLLine(account_id=inv_id, denomination="DUAL", base_debit=cost,
                                   metal_debit_grams=grams, karat=kar, memo=f"opening {label} {r.code}"))
            equity_credit += cost
            metal_net_by_karat[kar] = metal_net_by_karat.get(kar, Decimal("0")) + grams

    # 2. Supplier balances → AP (cash) and Metal AP (grams), CR liabilities.
    sup_balances = (await db.execute(select(SupplierBalance))).scalars().all()
    for bal in sup_balances:
        if bal.balance == 0:
            continue
        if bal.unit == DebtUnit.CASH:
            lines.append(gl.GLLine(account_id=ap_id, denomination="MONEY",
                                   base_credit=bal.balance, money_credit=bal.balance,
                                   memo="opening AP"))
            equity_credit -= bal.balance
        else:  # GOLD — credit Metal AP grams; the liability finances part of the on-hand metal
            kar = bal.karat or "K21"
            lines.append(gl.GLLine(account_id=metal_ap_id, denomination="DUAL",
                                   metal_credit_grams=bal.balance, karat=kar,
                                   memo="opening metal AP"))
            metal_net_by_karat[kar] = metal_net_by_karat.get(kar, Decimal("0")) - bal.balance

    # 3. Manual cash/bank, DR. Each cash line resolves by account_id or system_key.
    for cl in cash_lines:
        if cl.get("account_id"):
            acct_id = cl["account_id"]
        else:
            acct_id = await _key_to_account_id(db, cl["system_key"])
        amt = Decimal(str(cl["amount"]))
        lines.append(gl.GLLine(account_id=acct_id, denomination="MONEY",
                               base_debit=amt, money_debit=amt, memo="opening cash"))
        equity_credit += amt

    # 4a. Equity money plug so the money dimension balances.
    if equity_credit > 0:
        lines.append(gl.GLLine(account_id=equity_id, denomination="DUAL",
                               base_credit=equity_credit, money_credit=equity_credit,
                               memo="opening balance equity"))
    elif equity_credit < 0:
        lines.append(gl.GLLine(account_id=equity_id, denomination="DUAL",
                               base_debit=-equity_credit, money_debit=-equity_credit,
                               memo="opening balance equity"))

    # 4b. Equity metal plug per karat so each karat's metal dimension balances.
    for kar, net in metal_net_by_karat.items():
        if net > 0:
            lines.append(gl.GLLine(account_id=equity_id, denomination="DUAL",
                                   metal_credit_grams=net, karat=kar,
                                   memo="opening metal equity"))
        elif net < 0:
            lines.append(gl.GLLine(account_id=equity_id, denomination="DUAL",
                                   metal_debit_grams=-net, karat=kar,
                                   memo="opening metal equity (net owed)"))

    return await gl.post_entry(
        db, entry_date=as_of, memo="Opening balances",
        source_type=gl.SOURCE_OPENING, source_id=None,
        lines=lines, actor_user_id=actor_user_id,
    )
