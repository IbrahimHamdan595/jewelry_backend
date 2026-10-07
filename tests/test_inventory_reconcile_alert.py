"""Supplier-balance reconcile: the Discord alert names each drift correctly.

Karat is stored and returned already prefixed ("K21"), so the alert line must
not add a second K.
"""
from decimal import Decimal

import pytest

from app.api import inventory
from app.models import DebtUnit, Supplier, SupplierBalance


@pytest.mark.asyncio
async def test_gold_drift_alert_names_the_karat_once(db, monkeypatch):
    supplier = Supplier(name="Beirut Bullion")
    db.add(supplier)
    await db.flush()
    # A stored gold balance with no purchases behind it: a drift by construction.
    db.add(SupplierBalance(supplier_id=supplier.id, unit=DebtUnit.GOLD, karat="K21", balance=Decimal("12.500")))
    await db.commit()

    sent: list[str] = []

    async def fake_alert(msg: str) -> None:
        sent.append(msg)

    monkeypatch.setattr(inventory, "send_discord_alert", fake_alert)

    result = await inventory.reconcile(alert=True, db=db, _=None)

    assert result["drift_count"] == 1
    assert result["supplier_balance_drifts"][0]["karat"] == "K21"
    assert result["discord_alerted"] is True
    assert len(sent) == 1
    assert "Beirut Bullion (GOLD K21)" in sent[0]
    assert "KK21" not in sent[0]
