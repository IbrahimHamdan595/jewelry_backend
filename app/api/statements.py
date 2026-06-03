from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import kpis as kpis_core
from app.core import statements
from app.core.permissions import require_accounting
from app.core.xlsx import Sheet, build_xlsx_response
from app.deps import get_db
from app.models import User

router = APIRouter(prefix="/accounting/statements", tags=["accounting-statements"])


def _S(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, dict):
        return {k: _S(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_S(x) for x in v]
    return v


# ── sheet builders (shared by JSON xlsx path; keep JSON and xlsx in lockstep) ──

def _pnl_sheets(d: dict) -> list[Sheet]:
    rows = []
    for label, lines in (("Revenue", d["revenue_lines"]), ("COGS", d["cogs_lines"]),
                         ("Operating expenses", d["opex_lines"])):
        rows.append([label, "", ""])
        for l in lines:
            rows.append([l["name"], l["code"], l["amount"]])
    rows += [["Revenue", "", d["revenue"]], ["COGS", "", d["cogs"]],
             ["Gross profit", "", d["gross_profit"]],
             ["Operating expenses", "", d["operating_expenses"]],
             ["Net profit", "", d["net_profit"]]]
    return [Sheet(name="P&L", headers=["Account", "Code", "Amount"], rows=rows,
                  title=f"Income Statement {d['start']} → {d['end']}")]


def _bs_sheets(d: dict) -> list[Sheet]:
    rows = [["ASSETS", ""]]
    rows += [[l["name"], l["amount"]] for l in d["asset_lines"]]
    rows += [["Total assets", d["total_assets"]], ["LIABILITIES", ""]]
    rows += [[l["name"], l["amount"]] for l in d["liability_lines"]]
    rows += [["Total liabilities", d["total_liabilities"]], ["EQUITY", ""]]
    rows += [[l["name"], l["amount"]] for l in d["equity_lines"]]
    rows += [["Total equity", d["total_equity"]]]
    sheets = [Sheet(name="Balance Sheet", headers=["Account", "Amount"], rows=rows,
                    title=f"Balance Sheet as of {d['as_of']}")]
    if d["metal_position"]:
        sheets.append(Sheet(name="Metal Position",
                            headers=["Karat", "Net grams"],
                            rows=[[m["karat"], m["net_grams"]] for m in d["metal_position"]],
                            title="Metal position (grams per karat)"))
    return sheets


def _cf_sheets(d: dict) -> list[Sheet]:
    rows = [["Opening cash", d["opening_cash"]]]
    rows += [[c["label"], c["amount"]] for c in d["categories"]]
    rows += [["Net change", d["net_change"]], ["Closing cash", d["closing_cash"]]]
    return [Sheet(name="Cash Flow", headers=["Item", "Amount"], rows=rows,
                  title=f"Cash Flow {d['start']} → {d['end']}")]


def _acct_sheets(d: dict) -> list[Sheet]:
    rows = [["Opening balance", "", "", d["opening_balance"]]]
    for r in d["rows"]:
        rows.append([r["entry_no"], str(r["date"]), r["memo"],
                     r.get("debit"), r.get("credit"), r["running_balance"]])
    rows.append(["Closing balance", "", "", d["closing_balance"]])
    return [Sheet(name="Account", headers=["Entry", "Date", "Memo", "Debit/Open", "Credit", "Balance"],
                  rows=rows, title=f"{d['code']} {d['name']} — {d['start']} → {d['end']}")]


_KPI_ROWS = [
    ("dsi", "DSI (days)", lambda d: f"avg inv {d['avg_inventory']} / COGS {d['cogs']}"),
    ("inventory_turnover", "Inventory turnover", lambda d: f"COGS {d['cogs']} / avg inv {d['avg_inventory']}"),
    ("dpo", "DPO (days)", lambda d: f"avg AP {d['avg_ap']} / COGS {d['cogs']}"),
    ("gross_margin", "Gross margin (%)", lambda d: f"gross {d['gross_profit']} / rev {d['revenue']}"),
    ("net_margin", "Net margin (%)", lambda d: f"net {d['net_profit']} / rev {d['revenue']}"),
    ("metal_turnover", "Metal turnover (grams)", lambda d: f"metal COGS {d['metal_cogs_grams']}g / avg {d['avg_metal_grams']}g"),
    ("dso", "DSO (days)", lambda d: f"avg AR {d['avg_ar']} / credit sales {d['credit_sales']}"),
    ("ccc", "Cash Conversion Cycle (days)", lambda d: f"DSO {d['dso']} + DSI {d['dsi']} − DPO {d['dpo']}"),
    ("current_ratio", "Current ratio", lambda d: f"assets {d['current_assets']} / liab {d['current_liabilities']}"),
    ("quick_ratio", "Quick ratio", lambda d: f"(assets {d['current_assets']} − inv {d['inventory']}) / liab {d['current_liabilities']}"),
]


def _kpis_sheets(d: dict) -> list[Sheet]:
    rows = []
    for key, label, inputs in _KPI_ROWS:
        kd = d[key]
        value = kd["value"]
        rows.append([label, ("n/a" if value is None else value), inputs(kd)])
    return [Sheet(name="KPIs", headers=["KPI", "Value", "Inputs"], rows=rows,
                  title=f"Financial KPIs {d['start']} → {d['end']} ({d['days']} days)")]


@router.get("/income-statement")
async def income_statement(start: date, end: date, format: str = Query(None),
                           db: AsyncSession = Depends(get_db),
                           _: User = Depends(require_accounting)):
    data = await statements.income_statement(db, start=start, end=end)
    if format == "xlsx":
        return build_xlsx_response(_pnl_sheets(data), filename=f"income-statement-{start}-{end}")
    return _S(data)


@router.get("/balance-sheet")
async def balance_sheet(as_of: date, format: str = Query(None),
                        db: AsyncSession = Depends(get_db),
                        _: User = Depends(require_accounting)):
    data = await statements.balance_sheet(db, as_of=as_of)
    if format == "xlsx":
        return build_xlsx_response(_bs_sheets(data), filename=f"balance-sheet-{as_of}")
    return _S(data)


@router.get("/cash-flow")
async def cash_flow(start: date, end: date, format: str = Query(None),
                    db: AsyncSession = Depends(get_db),
                    _: User = Depends(require_accounting)):
    data = await statements.cash_flow(db, start=start, end=end)
    if format == "xlsx":
        return build_xlsx_response(_cf_sheets(data), filename=f"cash-flow-{start}-{end}")
    return _S(data)


@router.get("/account-statement")
async def account_statement(account_id: str, start: date, end: date, format: str = Query(None),
                            db: AsyncSession = Depends(get_db),
                            _: User = Depends(require_accounting)):
    data = await statements.account_statement(db, account_id=account_id, start=start, end=end)
    if format == "xlsx":
        return build_xlsx_response(_acct_sheets(data), filename=f"account-{data['code']}-{start}-{end}")
    return _S(data)


@router.get("/kpis")
async def kpis(start: date, end: date, format: str = Query(None),
               db: AsyncSession = Depends(get_db),
               _: User = Depends(require_accounting)):
    data = await kpis_core.compute_kpis(db, start=start, end=end)
    if format == "xlsx":
        return build_xlsx_response(_kpis_sheets(data), filename=f"kpis-{start}-{end}")
    return _S(data)
