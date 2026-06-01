"""Phase 5 — calendar (day/month/year) filtering on the orders list, timezone-
correct for Beirut. Checkpoints 5.1, 5.2."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.api.orders import list_orders
from app.models import Order, OrderStatus, PaymentMethod, Role, User


async def _seed(db):
    admin = User(id="admin1", email="a@x.com", name="Admin", password_hash="x", role=Role.ADMIN)
    db.add(admin)

    def mk(oid, dt):
        return Order(
            id=oid, order_number=oid, status=OrderStatus.COMPLETED,
            payment_method=PaymentMethod.CASH, cashier_id="admin1",
            subtotal=Decimal("100"), vat_percent=Decimal("11"), vat_amount=Decimal("11"),
            total_usd=Decimal("111"), total_lbp=Decimal("0"), lbp_exchange_rate=Decimal("89500"),
            created_at=dt,
        )

    # Beirut 2026-07-15 spans UTC [2026-07-14 21:00, 2026-07-15 21:00).
    db.add(mk("IN_DAY", datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)))    # in the day
    db.add(mk("EARLY", datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)))     # previous Beirut day
    db.add(mk("NEXT_MONTH", datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))) # different month
    await db.commit()
    return admin


@pytest.mark.asyncio
async def test_5_1_day_filter_timezone_correct(db):
    admin = await _seed(db)
    out = await list_orders(granularity="day", date="2026-07-15", page=1, page_size=50, db=db, _=admin)
    ids = {o.order_number for o in out.items}
    assert ids == {"IN_DAY"}  # EARLY is the prior Beirut day, NEXT_MONTH excluded


@pytest.mark.asyncio
async def test_5_2_month_filter_aggregates(db):
    admin = await _seed(db)
    out = await list_orders(granularity="month", date="2026-07-01", page=1, page_size=50, db=db, _=admin)
    ids = {o.order_number for o in out.items}
    assert ids == {"IN_DAY", "EARLY"}  # both July (Beirut), August excluded


@pytest.mark.asyncio
async def test_5_2b_year_filter(db):
    admin = await _seed(db)
    out = await list_orders(granularity="year", date="2026-01-01", page=1, page_size=50, db=db, _=admin)
    assert out.total == 3  # all three in 2026


@pytest.mark.asyncio
async def test_5_bad_date_is_422(db):
    from fastapi import HTTPException

    admin = await _seed(db)
    with pytest.raises(HTTPException) as exc:
        await list_orders(granularity="day", date="not-a-date", page=1, page_size=50, db=db, _=admin)
    assert exc.value.status_code == 422
