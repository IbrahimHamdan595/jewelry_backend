"""NEX-48 — revenue / average on the orders list. The revenue aggregate used to
sum the outer `orders` table while selecting from the filtered subquery: a
cartesian product that counted every order (voided and refunded included) once
per completed order and ignored the list filters."""
import warnings
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import SAWarning

from app.api.orders import list_orders
from app.models import Order, OrderStatus, PaymentMethod, Role, User


def _order(oid, status, total, dt, *, cashier="admin1", payment=PaymentMethod.CASH):
    return Order(
        id=oid, order_number=oid, status=status,
        payment_method=payment, cashier_id=cashier,
        subtotal=Decimal(total), vat_percent=Decimal("0"), vat_amount=Decimal("0"),
        total_usd=Decimal(total), total_lbp=Decimal("0"), lbp_exchange_rate=Decimal("89500"),
        created_at=dt,
    )


JULY_15 = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)
AUG_2 = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)


async def _seed(db, *, completed=True):
    admin = User(id="admin1", email="a@x.com", name="Admin", password_hash="x", role=Role.ADMIN)
    cashier = User(id="cash2", email="c@x.com", name="Cashier", password_hash="x", role=Role.CASHIER)
    db.add_all([admin, cashier])

    # Voided / refunded totals are deliberately large: any leak is unmissable.
    db.add(_order("VOID", OrderStatus.VOIDED, "1000.00", JULY_15))
    db.add(_order("REFUND", OrderStatus.REFUNDED, "2000.00", JULY_15))
    if completed:
        # Hand-summed: 100.00 + 250.50 + 99.50 = 450.00 over 3 orders → avg 150.00.
        db.add(_order("C1", OrderStatus.COMPLETED, "100.00", JULY_15))
        db.add(_order("C2", OrderStatus.COMPLETED, "250.50", JULY_15, payment=PaymentMethod.CARD))
        db.add(_order("C3", OrderStatus.COMPLETED, "99.50", AUG_2, cashier="cash2"))
    await db.commit()
    return admin


@pytest.mark.asyncio
async def test_revenue_sums_completed_orders_only(db):
    admin = await _seed(db)
    out = await list_orders(page=1, page_size=50, db=db, _=admin)
    assert out.total == 5  # the list still shows every order
    assert out.total_revenue == Decimal("450.00")  # voided + refunded contribute nothing
    assert out.avg_order_value == Decimal("150.00")  # 450.00 / 3 completed, not / 5 listed


@pytest.mark.asyncio
async def test_revenue_respects_calendar_filter(db):
    admin = await _seed(db)
    out = await list_orders(granularity="day", date="2026-07-15", page=1, page_size=50, db=db, _=admin)
    assert out.total == 4  # C1, C2, VOID, REFUND — C3 is in August
    assert out.total_revenue == Decimal("350.50")  # 100.00 + 250.50
    assert out.avg_order_value == Decimal("175.25")  # 350.50 / 2


@pytest.mark.asyncio
async def test_revenue_respects_legacy_date_range(db):
    admin = await _seed(db)
    out = await list_orders(
        from_date="2026-08-01T00:00:00+00:00", to_date="2026-08-31T00:00:00+00:00",
        page=1, page_size=50, db=db, _=admin,
    )
    assert {o.order_number for o in out.items} == {"C3"}
    assert out.total_revenue == Decimal("99.50")
    assert out.avg_order_value == Decimal("99.50")


@pytest.mark.asyncio
async def test_revenue_respects_cashier_filter(db):
    admin = await _seed(db)
    out = await list_orders(cashier="cash2", page=1, page_size=50, db=db, _=admin)
    assert {o.order_number for o in out.items} == {"C3"}
    assert out.total_revenue == Decimal("99.50")
    assert out.avg_order_value == Decimal("99.50")


@pytest.mark.asyncio
async def test_revenue_respects_payment_filter(db):
    admin = await _seed(db)
    out = await list_orders(payment="CARD", page=1, page_size=50, db=db, _=admin)
    assert {o.order_number for o in out.items} == {"C2"}
    assert out.total_revenue == Decimal("250.50")
    assert out.avg_order_value == Decimal("250.50")


@pytest.mark.asyncio
async def test_status_completed_filter_matches_unfiltered_revenue(db):
    admin = await _seed(db)
    out = await list_orders(status="COMPLETED", page=1, page_size=50, db=db, _=admin)
    assert out.total == 3
    assert out.total_revenue == Decimal("450.00")
    assert out.avg_order_value == Decimal("150.00")


@pytest.mark.asyncio
async def test_status_voided_filter_lists_orders_with_zero_revenue(db):
    admin = await _seed(db)
    out = await list_orders(status="VOIDED", page=1, page_size=50, db=db, _=admin)
    assert {o.order_number for o in out.items} == {"VOID"}
    assert out.total == 1
    assert out.total_revenue == Decimal("0")
    assert out.avg_order_value == Decimal("0")


@pytest.mark.asyncio
async def test_no_completed_orders_is_zero_not_a_division_error(db):
    admin = await _seed(db, completed=False)
    out = await list_orders(page=1, page_size=50, db=db, _=admin)
    assert out.total == 2  # VOID + REFUND are listed
    assert out.total_revenue == Decimal("0")
    assert out.avg_order_value == Decimal("0")


@pytest.mark.asyncio
async def test_revenue_query_has_no_cartesian_product(db):
    admin = await _seed(db)
    with warnings.catch_warnings():
        warnings.simplefilter("error", SAWarning)
        out = await list_orders(page=1, page_size=50, db=db, _=admin)
    assert out.total_revenue == Decimal("450.00")
