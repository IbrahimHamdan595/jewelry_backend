"""Integration test for coin/ounce stock reconcile (audit B1).

Proves the WHERE clauses in `_expected_unit_qty` correctly sum every event
that mutates `on_hand_qty` and exclude every event that doesn't.

Critical because this is the same class of risk as the zakat filter test —
a wrong WHERE clause produces a perfectly valid number computed over the
wrong rows, and no other test would catch it.
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.api.inventory import _expected_unit_qty, reconcile_units
from app.models import (
    AdjustmentReason,
    AdjustmentTarget,
    BuybackKind,
    BuybackPriceMode,
    CoinType,
    Karat,
    MarginMode,
    ManualAdjustment,
    Order,
    OrderItem,
    OrderItemKind,
    OrderStatus,
    OunceType,
    PaymentMethod,
    Role,
    Supplier,
    SupplierItemKind,
    SupplierPurchase,
    SupplierPurchaseItem,
    SupplierPurchaseMode,
    User,
    WalkinBuyback,
)


def _make_user() -> User:
    return User(
        id="u-admin",
        email="t@t.t",
        name="t",
        password_hash="x",
        role=Role.ADMIN,
        is_active=True,
    )


def _make_coin(code: str, *, on_hand_qty: int) -> CoinType:
    return CoinType(
        code=code,
        name_en=code,
        karat=Karat.K22,
        weight_grams=Decimal("7.988"),
        markup_per_gram=Decimal("0"),
        margin_mode=MarginMode.USD,
        margin_value=Decimal("0"),
        on_hand_qty=on_hand_qty,
        is_active=True,
    )


@pytest.mark.asyncio
async def test_expected_qty_sums_every_source_correctly(db):
    """Seed one coin type with a mix of events that should sum to a known
    expected qty, and confirm `_expected_unit_qty` produces it exactly.

    Seed plan for coin "C-MIX":
      +20 supplier purchase
      +1  buyback (walkin)
      -2  manual adjustment (shrinkage)
      +5  manual adjustment (correction-up)
      -7  sold in a COMPLETED order
      -1  sold in a REFUNDED order (still subtracts — refund didn't restore)
      ±0  sold then VOIDED order (excluded entirely from the sum)
      → expected = 20 + 1 - 2 + 5 - 7 - 1 = 16
    """
    user = _make_user()
    coin = _make_coin("C-MIX", on_hand_qty=0)  # stored qty intentionally 0 — we'll assert drift = 0 - 16 = -16
    db.add(user)
    db.add(coin)
    await db.flush()

    # Supplier purchase: +20
    supplier = Supplier(name="Acme")
    db.add(supplier)
    await db.flush()
    sp = SupplierPurchase(
        supplier_id=supplier.id,
        payment_mode=SupplierPurchaseMode.CASH,
        total_cash_due=Decimal("0"),
        total_grams_due_by_karat={},
        cash_paid_at_creation=Decimal("0"),
        grams_paid_at_creation_by_karat={},
        created_by_user_id=user.id,
    )
    db.add(sp)
    await db.flush()
    db.add(SupplierPurchaseItem(
        purchase_id=sp.id,
        item_kind=SupplierItemKind.COIN,
        coin_type_id=coin.id,
        quantity=20,
        karat=Karat.K22,
        unit_cost_usd=Decimal("0"),
    ))

    # Walkin buyback: +1
    db.add(WalkinBuyback(
        seller_name="x", seller_phone="x",
        cashier_id=user.id,
        kind=BuybackKind.COIN,
        coin_type_id=coin.id,
        quantity=1,
        weight_grams=Decimal("7.988"),
        karat=Karat.K22,
        buy_price_usd=Decimal("0"),
        gold_rate_at_buy=Decimal("100"),
        price_mode=BuybackPriceMode.FORMULA,
    ))

    # Manual adjustments: -2 (shrinkage), +5 (correction-up)
    db.add(ManualAdjustment(
        target_type=AdjustmentTarget.COIN_STOCK,
        target_id=coin.id,
        delta=Decimal("-2"),
        reason=AdjustmentReason.LOSS,
        notes="lost",
        actor_user_id=user.id,
    ))
    db.add(ManualAdjustment(
        target_type=AdjustmentTarget.COIN_STOCK,
        target_id=coin.id,
        delta=Decimal("5"),
        reason=AdjustmentReason.CORRECTION,
        notes="found in safe",
        actor_user_id=user.id,
    ))

    # Three orders: -7 (COMPLETED), -1 (REFUNDED), and one VOIDED that should be excluded
    for status, qty, order_num in [
        (OrderStatus.COMPLETED, 7, "ORD-1"),
        (OrderStatus.REFUNDED, 1, "ORD-2"),
        (OrderStatus.VOIDED, 100, "ORD-3"),  # large weight on a void to make leakage catastrophic
    ]:
        order = Order(
            order_number=order_num,
            status=status,
            payment_method=PaymentMethod.CASH,
            cashier_id=user.id,
            subtotal=Decimal("0"),
            vat_percent=Decimal("0"),
            vat_amount=Decimal("0"),
            total_usd=Decimal("0"),
            total_lbp=Decimal("0"),
            lbp_exchange_rate=Decimal("89500"),
        )
        db.add(order)
        await db.flush()
        db.add(OrderItem(
            order_id=order.id,
            item_kind=OrderItemKind.COIN,
            coin_type_id=coin.id,
            quantity=qty,
            product_code=coin.code,
            product_name=coin.name_en,
            karat=Karat.K22,
            weight_grams=Decimal("7.988"),
            gold_rate_at_sale=Decimal("100"),
            margin_percent=Decimal("0"),
            making_charge=Decimal("0"),
            final_price=Decimal("0"),
        ))

    await db.flush()

    expected = await _expected_unit_qty(db, kind="COIN", unit_type_id=coin.id)
    # 20 + 1 - 2 + 5 - 7 - 1 = 16; VOIDED's 100 must NOT leak in
    assert expected == 16

    # Sanity: if VOIDED leaked through (a wrong WHERE), expected would be
    # 16 - 100 = -84. The assertion above catches it; this is the extra
    # tripwire that makes the failure mode catastrophic instead of subtle.
    assert expected > 0


@pytest.mark.asyncio
async def test_reconcile_reports_drift_when_stored_disagrees(db):
    """End-to-end: stored qty differs from the replay → drift appears in
    the endpoint output with the correct sign."""
    user = _make_user()
    coin = _make_coin("C-DRIFT", on_hand_qty=10)  # stored = 10
    db.add(user)
    db.add(coin)
    await db.flush()

    # One supplier purchase of 8. Expected = 8, stored = 10. Drift = +2.
    supplier = Supplier(name="X")
    db.add(supplier)
    await db.flush()
    sp = SupplierPurchase(
        supplier_id=supplier.id,
        payment_mode=SupplierPurchaseMode.CASH,
        total_cash_due=Decimal("0"),
        total_grams_due_by_karat={},
        cash_paid_at_creation=Decimal("0"),
        grams_paid_at_creation_by_karat={},
        created_by_user_id=user.id,
    )
    db.add(sp)
    await db.flush()
    db.add(SupplierPurchaseItem(
        purchase_id=sp.id,
        item_kind=SupplierItemKind.COIN,
        coin_type_id=coin.id,
        quantity=8,
        karat=Karat.K22,
        unit_cost_usd=Decimal("0"),
    ))
    await db.flush()

    result = await reconcile_units(alert=False, db=db, _=user)

    assert result["drift_count"] == 1
    d = result["unit_drifts"][0]
    assert d["kind"] == "COIN"
    assert d["code"] == "C-DRIFT"
    assert d["stored"] == 10
    assert d["computed"] == 8
    assert d["drift"] == 2  # stored - computed: stored is 2 too high


@pytest.mark.asyncio
async def test_reconcile_clean_when_stored_matches_replay(db):
    """Zero-drift baseline: empty inventory and matching stored values."""
    user = _make_user()
    coin = _make_coin("C-CLEAN", on_hand_qty=0)
    ounce = OunceType(
        code="O-CLEAN", name_en="o", karat=Karat.K24, weight_grams=Decimal("31.104"),
        markup_per_gram=Decimal("0"), margin_mode=MarginMode.USD,
        margin_value=Decimal("0"), on_hand_qty=0, is_active=True,
    )
    db.add(user)
    db.add(coin)
    db.add(ounce)
    await db.flush()

    result = await reconcile_units(alert=False, db=db, _=user)
    assert result["drift_count"] == 0
    assert result["unit_drifts"] == []


@pytest.mark.asyncio
async def test_reconcile_isolates_per_unit_type(db):
    """A purchase of coin A must not leak into coin B's expected qty."""
    user = _make_user()
    coin_a = _make_coin("C-A", on_hand_qty=5)  # stored 5, will have +5 in events
    coin_b = _make_coin("C-B", on_hand_qty=100)  # stored 100, will have 0 events → drift +100
    db.add(user)
    db.add(coin_a)
    db.add(coin_b)
    await db.flush()

    supplier = Supplier(name="X")
    db.add(supplier)
    await db.flush()
    sp = SupplierPurchase(
        supplier_id=supplier.id,
        payment_mode=SupplierPurchaseMode.CASH,
        total_cash_due=Decimal("0"),
        total_grams_due_by_karat={},
        cash_paid_at_creation=Decimal("0"),
        grams_paid_at_creation_by_karat={},
        created_by_user_id=user.id,
    )
    db.add(sp)
    await db.flush()
    db.add(SupplierPurchaseItem(
        purchase_id=sp.id,
        item_kind=SupplierItemKind.COIN,
        coin_type_id=coin_a.id,   # ← only A
        quantity=5,
        karat=Karat.K22,
        unit_cost_usd=Decimal("0"),
    ))
    await db.flush()

    result = await reconcile_units(alert=False, db=db, _=user)
    # A: stored 5, expected 5 → no drift.
    # B: stored 100, expected 0 → drift +100.
    assert result["drift_count"] == 1
    assert result["unit_drifts"][0]["code"] == "C-B"
    assert result["unit_drifts"][0]["drift"] == 100
