from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.core import dashboard
from app.core.daterange import BEIRUT_TZ, day_range


def test_dashboard_module_importable():
    assert hasattr(dashboard, "week_window")


def test_week_window_is_seven_beirut_days():
    start, end = dashboard.week_window(date(2026, 6, 5))
    assert end == day_range(date(2026, 6, 5))[1]
    assert start == day_range(date(2026, 5, 30))[0]


# ── shared fixtures ───────────────────────────────────────────────────────────
from app.models import (
    Karat, Order, OrderItem, OrderItemKind, OrderStatus, PaymentMethod, User,
)

_SEQ = [0]


async def _ensure_user(db):
    u = (await db.execute(select(User).where(User.id == "u1"))).scalar_one_or_none()
    if u is None:
        db.add(User(id="u1", name="C", email="c@x.co", password_hash="x", role="ADMIN"))
        await db.flush()


async def _order(db, *, total, when, status=OrderStatus.COMPLETED, items=()):
    await _ensure_user(db)
    _SEQ[0] += 1
    o = Order(cashier_id="u1", order_number=f"O{_SEQ[0]}", status=status,
              payment_method=PaymentMethod.CASH, subtotal=total, vat_percent=D("0"),
              vat_amount=D("0"), total_usd=total, total_lbp=D("0"),
              lbp_exchange_rate=D("89500"), created_at=when)
    db.add(o)
    await db.flush()
    for it in items:
        db.add(OrderItem(order_id=o.id, item_kind=OrderItemKind.PRODUCT, quantity=it["qty"],
                         product_code="P", product_name="P", karat=it["karat"],
                         weight_grams=it["grams"], gold_rate_at_sale=D("80"),
                         margin_percent=D("0"), making_charge=it["making"],
                         final_price=it["final"]))
    await db.flush()
    return o


# ── Phase A ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gold_weight_sold_by_karat(db):
    now = day_range(date(2026, 6, 5))[0] + timedelta(hours=12)
    await _order(db, total=D("500"), when=now, items=[
        {"qty": 2, "karat": Karat.K21, "grams": D("5"), "making": D("25"), "final": D("250")},
        {"qty": 1, "karat": Karat.K18, "grams": D("3"), "making": D("25"), "final": D("100")},
    ])
    rows = await dashboard.gold_weight_sold_by_karat(db, *day_range(date(2026, 6, 5)))
    by = {r["karat"]: r["grams"] for r in rows}
    assert by["K21"] == D("10.000")
    assert by["K18"] == D("3.000")


@pytest.mark.asyncio
async def test_making_charges_and_avg_invoice(db):
    now = day_range(date(2026, 6, 5))[0] + timedelta(hours=12)
    await _order(db, total=D("300"), when=now, items=[
        {"qty": 2, "karat": Karat.K21, "grams": D("5"), "making": D("25"), "final": D("250")}])
    making = await dashboard.making_charges(db, *day_range(date(2026, 6, 5)))
    assert making == D("50.00")
    assert dashboard.avg_invoice(D("300"), 1) == D("300.00")
    assert dashboard.avg_invoice(D("0"), 0) == D("0.00")
