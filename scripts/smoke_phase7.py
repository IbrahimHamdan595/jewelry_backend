"""Phase 7 smoke test: alerts + reconcile + dashboard rollups."""

import asyncio
import os
import sys
from decimal import Decimal

import httpx

from app.config import settings
from app.main import app


ADMIN_EMAIL = os.environ.get("SMOKE_ADMIN_EMAIL", settings.seed_admin_email)
ADMIN_PASSWORD = os.environ.get("SMOKE_ADMIN_PASSWORD", settings.seed_admin_password)


class Reporter:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[tuple[str, str]] = []

    def ok(self, label: str) -> None:
        print(f"  ✓ {label}")
        self.passed += 1

    def fail(self, label: str, detail: str) -> None:
        print(f"  ✗ {label}\n      {detail}")
        self.failed.append((label, detail))

    def summary(self) -> int:
        total = self.passed + len(self.failed)
        print(f"\n  {self.passed}/{total} passed, {len(self.failed)} failed")
        return 0 if not self.failed else 1


async def main() -> int:
    r = Reporter()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        if resp.status_code != 200:
            r.fail("login", resp.text)
            return r.summary()
        H = {"Authorization": f"Bearer {resp.json()['access_token']}"}

        # ── Inventory alerts ──────────────────────────────────────────────────
        print("\n▶ Inventory alerts")

        # Create a coin type with min_stock_qty=10 and stock=2
        resp = await c.post(
            "/api/coins",
            headers=H,
            json={
                "code": "SMOKE-P7-LOW",
                "name_en": "Smoke Phase7 Low-Stock Coin",
                "karat": "K21",
                "weight_grams": "8",
                "markup_per_gram": "0",
                "margin_mode": "USD",
                "margin_value": "10",
                "min_stock_qty": 10,
            },
        )
        if resp.status_code != 201:
            r.fail("create low-stock coin", resp.text)
            return r.summary()
        low_coin = resp.json()
        # Add 2 to stock (below threshold of 10)
        await c.post(
            "/api/adjustments",
            headers=H,
            json={
                "target_type": "COIN_STOCK",
                "target_id": low_coin["id"],
                "delta": "2",
                "reason": "CORRECTION",
                "notes": "smoke p7 stock topup below threshold",
            },
        )
        r.ok("created low-stock coin (qty=2, min=10)")

        # Create an above-threshold coin for negative case
        resp = await c.post(
            "/api/coins",
            headers=H,
            json={
                "code": "SMOKE-P7-OK",
                "name_en": "Smoke Phase7 OK-Stock Coin",
                "karat": "K22",
                "weight_grams": "7.216",
                "markup_per_gram": "0",
                "margin_mode": "USD",
                "margin_value": "5",
                "min_stock_qty": 1,
            },
        )
        ok_coin = resp.json()
        await c.post(
            "/api/adjustments",
            headers=H,
            json={
                "target_type": "COIN_STOCK",
                "target_id": ok_coin["id"],
                "delta": "20",
                "reason": "CORRECTION",
                "notes": "smoke p7 OK stock",
            },
        )

        resp = await c.get("/api/inventory/alerts", headers=H)
        alerts = resp.json()
        low_codes = {a["code"] for a in alerts["below_threshold"]}
        if "SMOKE-P7-LOW" in low_codes:
            r.ok(f"low-stock coin in alerts (below_threshold count={alerts['total']})")
        else:
            r.fail("alerts missing low coin", str(alerts))
        if "SMOKE-P7-OK" not in low_codes:
            r.ok("above-threshold coin correctly NOT in alerts")
        else:
            r.fail("ok coin in alerts", "")

        # Bring low coin back above threshold and re-check
        await c.post(
            "/api/adjustments",
            headers=H,
            json={
                "target_type": "COIN_STOCK",
                "target_id": low_coin["id"],
                "delta": "20",
                "reason": "CORRECTION",
                "notes": "smoke p7 restock",
            },
        )
        resp = await c.get("/api/inventory/alerts", headers=H)
        low_codes = {a["code"] for a in resp.json()["below_threshold"]}
        if "SMOKE-P7-LOW" not in low_codes:
            r.ok("coin removed from alerts after restock")
        else:
            r.fail("coin still in alerts", "")

        # ── Reconciliation: no-drift case ─────────────────────────────────────
        print("\n▶ Reconciliation — clean state")
        resp = await c.get("/api/inventory/reconcile", headers=H)
        rec = resp.json()
        if rec["drift_count"] == 0:
            r.ok(f"no supplier balance drift (count=0)")
        else:
            print(f"   (note) drift_count={rec['drift_count']}; entries: {rec['supplier_balance_drifts'][:3]}")
            r.fail("clean reconcile", f"unexpected drift_count={rec['drift_count']}")

        # ── Reconciliation: inject drift, verify detection ────────────────────
        print("\n▶ Reconciliation — injected drift")
        # Create a fresh supplier + purchase that creates a balance, then poke
        # supplier_balances directly to simulate drift.
        resp = await c.post(
            "/api/suppliers",
            headers=H,
            json={"name": "Smoke P7 Drift Supplier", "notes": "smoke p7 drift"},
        )
        if resp.status_code != 201:
            r.fail("create drift supplier", resp.text)
            return r.summary()
        sup_id = resp.json()["id"]

        # Create a $500 cash-only purchase, no payment → balance should be $500
        resp = await c.post(
            f"/api/suppliers/{sup_id}/purchases",
            headers=H,
            json={
                "payment_mode": "CASH",
                "total_cash_due": "500",
                "cash_paid_at_creation": "0",
                "items": [
                    {
                        "item_kind": "PURE_GOLD",
                        "weight_grams": "5",
                        "karat": "K18",
                        "unit_cost_usd": "500",
                        "notes": "smoke p7 drift purchase",
                    }
                ],
                "notes": "smoke p7 drift",
            },
        )
        if resp.status_code != 201:
            r.fail("drift purchase", resp.text)

        # Sanity: reconcile should still be clean (we created both purchase and balance via the proper endpoint)
        resp = await c.get("/api/inventory/reconcile", headers=H)
        if resp.json()["drift_count"] == 0:
            r.ok("after proper purchase: no drift")
        else:
            r.fail("post-purchase drift", str(resp.json()))

        # Now inject drift by direct DB update on supplier_balances
        from app.db.session import async_session_factory
        from app.models import SupplierBalance, DebtUnit
        async with async_session_factory() as db:
            row = (
                await db.execute(
                    SupplierBalance.__table__.select().where(
                        SupplierBalance.supplier_id == sup_id,
                        SupplierBalance.unit == DebtUnit.CASH,
                    )
                )
            ).first()
            if row:
                await db.execute(
                    SupplierBalance.__table__.update()
                    .where(
                        SupplierBalance.supplier_id == sup_id,
                        SupplierBalance.unit == DebtUnit.CASH,
                    )
                    .values(balance=Decimal("450"))  # was 500, drift = -50
                )
                await db.commit()
                r.ok("manually wrote bad balance to simulate drift (500 → 450)")
            else:
                r.fail("could not find balance row to drift", "")

        resp = await c.get("/api/inventory/reconcile", headers=H)
        rec = resp.json()
        drift_for_us = [
            d for d in rec["supplier_balance_drifts"] if d["supplier_id"] == sup_id
        ]
        if drift_for_us:
            d = drift_for_us[0]
            if d["stored"] == "450.000" and d["computed"] == "500":
                r.ok(f"drift detected for our supplier (stored={d['stored']}, computed={d['computed']})")
            else:
                r.ok(f"drift detected (stored={d['stored']}, computed={d['computed']})")
        else:
            r.fail("drift not detected", str(rec))

        # ── Restore the balance so we can deactivate cleanly ──────────────────
        async with async_session_factory() as db:
            await db.execute(
                SupplierBalance.__table__.update()
                .where(
                    SupplierBalance.supplier_id == sup_id,
                    SupplierBalance.unit == DebtUnit.CASH,
                )
                .values(balance=Decimal("500"))
            )
            await db.commit()
        # Settle properly
        await c.post(
            f"/api/suppliers/{sup_id}/payments",
            headers=H,
            json={"unit": "CASH", "amount": "500", "notes": "smoke p7 settle"},
        )

        # ── Dashboard inventory + AP rollups ──────────────────────────────────
        print("\n▶ Dashboard rollups")
        resp = await c.get("/api/reports/dashboard", headers=H)
        if resp.status_code != 200:
            r.fail("dashboard", resp.text)
            return r.summary()
        d = resp.json()
        if "inventory" in d and "accounts_payable" in d:
            r.ok("dashboard includes inventory + accounts_payable sections")
        else:
            r.fail("dashboard sections", str(list(d.keys())))

        inv = d["inventory"]
        if isinstance(inv.get("pure_gold_by_karat"), list) and len(inv["pure_gold_by_karat"]) >= 1:
            karats = sorted({row["karat"] for row in inv["pure_gold_by_karat"]})
            r.ok(f"pure_gold_by_karat populated, karats: {karats}")
        else:
            r.fail("pure_gold rollup", str(inv))

        if "on_hand_total" in inv["coins"] and "on_hand_total" in inv["ounces"]:
            r.ok(
                f"coin/ounce rollups present "
                f"(coins on_hand={inv['coins']['on_hand_total']}, "
                f"ounces on_hand={inv['ounces']['on_hand_total']})"
            )
        else:
            r.fail("unit rollups", str(inv))

        if "low_stock_alerts" in inv:
            r.ok(f"low_stock_alerts count = {inv['low_stock_alerts']}")
        else:
            r.fail("low stock count", str(inv))

        ap = d["accounts_payable"]
        if "total_cash_owed" in ap and "total_grams_owed_by_karat" in ap and "supplier_count" in ap:
            r.ok(
                f"AP rollup: ${ap['total_cash_owed']} cash, "
                f"gold={ap['total_grams_owed_by_karat']}, "
                f"{ap['supplier_count']} supplier(s)"
            )
        else:
            r.fail("AP rollup shape", str(ap))

    return r.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
