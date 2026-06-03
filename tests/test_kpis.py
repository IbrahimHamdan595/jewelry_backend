from datetime import date
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.core import gl, kpis
from app.core.coa_seed import seed_chart_of_accounts
from app.models import GLAccount, GLPeriod, PeriodStatus


async def _acct(db, system_key: str) -> str:
    return (await db.execute(
        select(GLAccount.id).where(GLAccount.system_key == system_key))).scalar_one()


async def _seed(db):
    await seed_chart_of_accounts(db)
    from app.core.bank import adopt_seeded_accounts
    await adopt_seeded_accounts(db)
    for m in (5, 6):
        db.add(GLPeriod(year=2026, period_no=m, status=PeriodStatus.OPEN))
    await db.flush()


async def _post(db, entry_date, lines):
    return await gl.post_entry(db, entry_date=entry_date, memo="t", source_type="TEST",
                               source_id=None, lines=lines, actor_user_id="u1")


def _m(account_id, *, debit=D("0"), credit=D("0")):
    return gl.GLLine(account_id=account_id, denomination="MONEY",
                     base_debit=debit, base_credit=credit,
                     money_debit=debit, money_credit=credit, currency="USD")


@pytest.mark.asyncio
async def test_money_kpis_on_known_fixture(db):
    await _seed(db)
    cash = await _acct(db, "CASH")
    rev = await _acct(db, "SALES_REVENUE")
    cogs = await _acct(db, "MAKING_COGS")
    inv = await _acct(db, "PRODUCT_INVENTORY")
    ar = await _acct(db, "AR")
    ap = await _acct(db, "AP")
    rent = await _acct(db, "RENT_EXPENSE")
    office = await _acct(db, "OFFICE_EXPENSE")
    obe = await _acct(db, "OPENING_BALANCE_EQUITY")

    # Before window (sets opening snapshot at 2026-05-31)
    await _post(db, date(2026, 5, 10), [_m(inv, debit=D("2000")), _m(obe, credit=D("2000"))])      # inventory 2000
    await _post(db, date(2026, 5, 10), [_m(ar, debit=D("500")), _m(rev, credit=D("500"))])         # AR 500
    await _post(db, date(2026, 5, 10), [_m(office, debit=D("300")), _m(ap, credit=D("300"))])      # AP 300

    # In window (June)
    await _post(db, date(2026, 6, 10), [_m(cash, debit=D("1200")), _m(rev, credit=D("1200"))])     # cash sale
    await _post(db, date(2026, 6, 10), [_m(cogs, debit=D("700")), _m(inv, credit=D("700"))])       # COGS 700
    await _post(db, date(2026, 6, 12), [_m(ar, debit=D("400")), _m(rev, credit=D("400"))])         # credit sale 400
    await _post(db, date(2026, 6, 15), [_m(rent, debit=D("100")), _m(cash, credit=D("100"))])      # rent 100

    k = await kpis.compute_kpis(db, start=date(2026, 6, 1), end=date(2026, 6, 30))
    assert k["days"] == 30
    # avg_inventory=(2000+1300)/2=1650 ; cogs=700
    assert k["dsi"]["value"] == D("70.71")
    assert k["inventory_turnover"]["value"] == D("0.42")
    # avg_ap=300 ; DPO=300/700*30
    assert k["dpo"]["value"] == D("12.86")
    # revenue=1600 gross=900 net=800
    assert k["gross_margin"]["value"] == D("56.25")
    assert k["net_margin"]["value"] == D("50.00")
    # avg_ar=(500+900)/2=700 ; credit_sales=400 ; DSO=700/400*30=52.5
    assert k["dso"]["value"] == D("52.50")
    # CCC = 52.5 + 70.714285 - 12.857142 = 110.357 -> 110.36
    assert k["ccc"]["value"] == D("110.36")
    # closing current_assets = cash 1100 + ar 900 + inv 1300 = 3300 ; current_liab = ap 300
    assert k["current_ratio"]["value"] == D("11.00")
    # quick = (3300-1300)/300 = 6.6667 -> 6.67
    assert k["quick_ratio"]["value"] == D("6.67")


@pytest.mark.asyncio
async def test_metal_turnover_uses_grams(db):
    await _seed(db)
    metal_inv = await _acct(db, "METAL_INVENTORY")
    metal_cogs = await _acct(db, "METAL_COGS")
    obe = await _acct(db, "OPENING_BALANCE_EQUITY")

    def dual(account_id, *, dr_g=D("0"), cr_g=D("0"), dr_m=D("0"), cr_m=D("0")):
        return gl.GLLine(account_id=account_id, denomination="DUAL",
                         base_debit=dr_m, base_credit=cr_m, money_debit=dr_m, money_credit=cr_m,
                         metal_debit_grams=dr_g, metal_credit_grams=cr_g, karat="K21", currency="USD")

    # Opening 100g K21 / 4000 (before window)
    await _post(db, date(2026, 5, 10), [dual(metal_inv, dr_g=D("100"), dr_m=D("4000")),
                                        dual(obe, cr_g=D("100"), cr_m=D("4000"))])
    # In window: metal COGS 40g / 1600
    await _post(db, date(2026, 6, 10), [dual(metal_cogs, dr_g=D("40"), dr_m=D("1600")),
                                        dual(metal_inv, cr_g=D("40"), cr_m=D("1600"))])

    k = await kpis.compute_kpis(db, start=date(2026, 6, 1), end=date(2026, 6, 30))
    # avg_metal=(100+60)/2=80 ; metal_cogs_grams=40 ; turnover=0.5
    assert k["metal_turnover"]["value"] == D("0.50")
    assert k["metal_turnover"]["metal_cogs_grams"] == D("40.000")


@pytest.mark.asyncio
async def test_kpi_guards_return_none_on_zero_denominators(db):
    await _seed(db)  # CoA only — no activity → all denominators zero
    k = await kpis.compute_kpis(db, start=date(2026, 6, 1), end=date(2026, 6, 30))
    assert k["dsi"]["value"] is None            # cogs 0
    assert k["inventory_turnover"]["value"] is None  # avg_inventory 0
    assert k["dpo"]["value"] is None            # cogs 0
    assert k["gross_margin"]["value"] is None   # revenue 0
    assert k["net_margin"]["value"] is None
    assert k["metal_turnover"]["value"] is None  # avg_metal 0
    assert k["dso"]["value"] is None            # credit_sales 0
    assert k["ccc"]["value"] is None            # depends on dso/dsi/dpo
    assert k["current_ratio"]["value"] is None  # current_liabilities 0
    assert k["quick_ratio"]["value"] is None


def test_kpis_sheet_builder_shape():
    from app.api.statements import _kpis_sheets

    data = {"start": date(2026, 6, 1), "end": date(2026, 6, 30), "days": 30,
            "dsi": {"value": D("70.71"), "avg_inventory": D("1650.00"), "cogs": D("700.00")},
            "inventory_turnover": {"value": D("0.42"), "avg_inventory": D("1650.00"), "cogs": D("700.00")},
            "dpo": {"value": D("12.86"), "avg_ap": D("300.00"), "cogs": D("700.00")},
            "gross_margin": {"value": D("56.25"), "gross_profit": D("900.00"), "revenue": D("1600.00")},
            "net_margin": {"value": D("50.00"), "net_profit": D("800.00"), "revenue": D("1600.00")},
            "metal_turnover": {"value": None, "avg_metal_grams": D("0.000"), "metal_cogs_grams": D("0.000")},
            "dso": {"value": D("52.50"), "avg_ar": D("700.00"), "credit_sales": D("400.00")},
            "ccc": {"value": D("110.36"), "dso": D("52.50"), "dsi": D("70.71"), "dpo": D("12.86")},
            "current_ratio": {"value": D("11.00"), "current_assets": D("3300.00"), "current_liabilities": D("300.00")},
            "quick_ratio": {"value": D("6.67"), "current_assets": D("3300.00"), "inventory": D("1300.00"), "current_liabilities": D("300.00")}}
    sheets = _kpis_sheets(data)
    assert sheets and sheets[0].name == "KPIs"
    assert sheets[0].headers == ["KPI", "Value", "Inputs"]
    labels = [r[0] for r in sheets[0].rows]
    assert "DSI (days)" in labels and "Cash Conversion Cycle (days)" in labels
    # None renders as "n/a"
    assert any(r[1] == "n/a" for r in sheets[0].rows)
