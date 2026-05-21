"""Phase 5 smoke test: suppliers + purchases + dual-unit debt + repayments + AP."""

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

        # ── Supplier CRUD ─────────────────────────────────────────────────────
        print("\n▶ Supplier CRUD")
        resp = await c.post(
            "/api/suppliers",
            headers=H,
            json={
                "name": "Smoke Phase5 Supplier",
                "contact_name": "Test Contact",
                "phone": "+961-00-555555",
                "payment_terms": "net 30, gold-for-gold preferred",
                "notes": "smoke phase 5",
            },
        )
        if resp.status_code != 201:
            r.fail("create supplier", f"{resp.status_code} {resp.text}")
            return r.summary()
        sup = resp.json()
        r.ok(f"POST /suppliers (id={sup['id'][:8]}…)")

        resp = await c.get("/api/suppliers", headers=H, params={"search": "Smoke"})
        if resp.status_code == 200 and resp.json()["total"] >= 1:
            r.ok("GET /suppliers search works")
        else:
            r.fail("list suppliers", resp.text)

        # ── Pre-stock a K21 lot so we can pay supplier in gold later ──────────
        resp = await c.post(
            "/api/lots",
            headers=H,
            json={
                "karat": "K21",
                "weight_grams": "200",
                "source": "SEED",
                "cost_basis_usd": "16000",
                "notes": "smoke phase5 stock lot",
            },
        )
        if resp.status_code != 201:
            r.fail("seed K21 stock lot", resp.text)
            return r.summary()
        stock_lot = resp.json()
        r.ok(f"seeded K21 200g lot for gold payments (id={stock_lot['id'][:8]}…)")

        # ── CASH-only purchase, partial settle ────────────────────────────────
        print("\n▶ CASH-only purchase, partial settle, then cash repayment")
        resp = await c.post(
            f"/api/suppliers/{sup['id']}/purchases",
            headers=H,
            json={
                "payment_mode": "CASH",
                "total_cash_due": "1000.00",
                "cash_paid_at_creation": "300.00",
                "items": [
                    {
                        "item_kind": "PURE_GOLD",
                        "weight_grams": "10",
                        "karat": "K21",
                        "unit_cost_usd": "1000",
                        "notes": "smoke phase5 cash purchase",
                    }
                ],
                "notes": "smoke phase5 cash purchase",
            },
        )
        if resp.status_code != 201:
            r.fail("cash purchase", f"{resp.status_code} {resp.text}")
            return r.summary()
        cash_purchase = resp.json()
        r.ok(f"POST cash purchase, items={len(cash_purchase['items'])}")

        resp = await c.get(f"/api/suppliers/{sup['id']}", headers=H)
        detail = resp.json()
        cash_balance = next(
            (Decimal(b["balance"]) for b in detail["balances"] if b["unit"] == "CASH"),
            None,
        )
        if cash_balance == Decimal("700"):
            r.ok(f"cash balance = $700 owed after $300 partial settle")
        else:
            r.fail("cash balance", f"expected 700, got {cash_balance}")

        # Partial repayment of $400
        resp = await c.post(
            f"/api/suppliers/{sup['id']}/payments",
            headers=H,
            json={"unit": "CASH", "amount": "400", "notes": "smoke partial repay"},
        )
        if resp.status_code != 201:
            r.fail("cash partial repay", f"{resp.status_code} {resp.text}")
        else:
            r.ok("POST cash repayment $400")

        resp = await c.get(f"/api/suppliers/{sup['id']}", headers=H)
        cash_balance = next(
            (Decimal(b["balance"]) for b in resp.json()["balances"] if b["unit"] == "CASH"),
            None,
        )
        if cash_balance == Decimal("300"):
            r.ok("cash balance = $300 after repayment")
        else:
            r.fail("cash balance after repay", f"expected 300, got {cash_balance}")

        # Overpayment rejection
        resp = await c.post(
            f"/api/suppliers/{sup['id']}/payments",
            headers=H,
            json={"unit": "CASH", "amount": "9999", "notes": "should reject"},
        )
        if resp.status_code == 422:
            r.ok("cash overpayment correctly rejected (422)")
        else:
            r.fail("cash overpay", f"expected 422, got {resp.status_code}")

        # ── MIXED purchase: cash + gold from stock lot ────────────────────────
        print("\n▶ MIXED purchase consuming K21 lot")
        resp = await c.post(
            f"/api/suppliers/{sup['id']}/purchases",
            headers=H,
            json={
                "payment_mode": "MIXED",
                "trade_markup_per_gram": "1.5",
                "total_cash_due": "200.00",
                "total_grams_due_by_karat": {"K21": "30.000"},
                "cash_paid_at_creation": "100.00",
                "gold_payments_at_creation": [
                    {"lot_id": stock_lot["id"], "grams": "10.000", "karat": "K21"}
                ],
                "items": [
                    {
                        "item_kind": "COIN",
                        # Pick any existing coin type with available stock by querying first below
                        "coin_type_id": None,
                        "quantity": None,
                        "unit_cost_usd": "0",
                    }
                ],
                "notes": "smoke phase5 mixed (items will be patched)",
            },
        )
        # That request will fail because we passed a null coin_type_id. Build a clean version:

        # Use a PURE_GOLD line for simplicity (creates a new lot from supplier)
        resp = await c.post(
            f"/api/suppliers/{sup['id']}/purchases",
            headers=H,
            json={
                "payment_mode": "MIXED",
                "trade_markup_per_gram": "1.5",
                "total_cash_due": "200.00",
                "total_grams_due_by_karat": {"K21": "30.000"},
                "cash_paid_at_creation": "100.00",
                "gold_payments_at_creation": [
                    {"lot_id": stock_lot["id"], "grams": "10.000", "karat": "K21"}
                ],
                "items": [
                    {
                        "item_kind": "PURE_GOLD",
                        "weight_grams": "5",
                        "karat": "K24",
                        "unit_cost_usd": "500",
                    }
                ],
                "notes": "smoke phase5 mixed",
            },
        )
        if resp.status_code != 201:
            r.fail("mixed purchase", f"{resp.status_code} {resp.text}")
        else:
            r.ok("POST mixed purchase ($200 cash + 30g K21)")
            mixed = resp.json()
            # Verify a SUPPLIER-source lot was created
            new_lot_id = mixed["items"][0]["lot_id"]
            if new_lot_id:
                lr = await c.get(f"/api/lots/{new_lot_id}", headers=H)
                if lr.json().get("source") == "SUPPLIER":
                    r.ok("PURE_GOLD item created a lot with source=SUPPLIER")
                else:
                    r.fail("supplier lot source", lr.text)

        resp = await c.get(f"/api/suppliers/{sup['id']}", headers=H)
        balances = {(b["unit"], b.get("karat")): Decimal(b["balance"]) for b in resp.json()["balances"]}
        # Cash debt: 300 (previous) + 100 (new mixed) = 400
        # Gold debt K21: 30 due - 10 paid = 20g K21
        if balances.get(("CASH", None)) == Decimal("400"):
            r.ok(f"cash balance = $400 (previous $300 + new $100)")
        else:
            r.fail("cash after mixed", f"expected 400, got {balances.get(('CASH', None))}")
        if balances.get(("GOLD", "K21")) == Decimal("20"):
            r.ok(f"gold K21 balance = 20g (30 due - 10 paid)")
        else:
            r.fail("gold K21 balance", f"expected 20, got {balances.get(('GOLD', 'K21'))}")

        # Confirm stock lot was debited
        resp = await c.get(f"/api/lots/{stock_lot['id']}", headers=H)
        remaining = Decimal(resp.json()["weight_remaining_grams"])
        if remaining == Decimal("190"):
            r.ok(f"stock lot debited correctly: 200 → 190g")
        else:
            r.fail("stock lot remaining", f"expected 190, got {remaining}")

        # ── GOLD repayment from a different lot ───────────────────────────────
        print("\n▶ GOLD repayment")
        resp = await c.post(
            f"/api/suppliers/{sup['id']}/payments",
            headers=H,
            json={
                "unit": "GOLD",
                "karat": "K21",
                "amount": "15",
                "gold_payments": [
                    {"lot_id": stock_lot["id"], "grams": "15.000", "karat": "K21"}
                ],
                "notes": "smoke phase5 gold repay 15g K21",
            },
        )
        if resp.status_code == 422 and "exceeds outstanding balance" in resp.text:
            r.ok("gold overpayment (15 > 20) — wait no, 15 < 20 should pass; trying again")
        if resp.status_code != 201:
            r.fail("gold repay", f"{resp.status_code} {resp.text}")
        else:
            r.ok("POST gold repayment 15g K21")

        resp = await c.get(f"/api/suppliers/{sup['id']}", headers=H)
        balances = {(b["unit"], b.get("karat")): Decimal(b["balance"]) for b in resp.json()["balances"]}
        if balances.get(("GOLD", "K21")) == Decimal("5"):
            r.ok("gold K21 balance = 5g remaining after 15g repay")
        else:
            r.fail("gold K21 after repay", f"expected 5, got {balances.get(('GOLD', 'K21'))}")

        # Gold overpayment
        resp = await c.post(
            f"/api/suppliers/{sup['id']}/payments",
            headers=H,
            json={
                "unit": "GOLD",
                "karat": "K21",
                "amount": "100",
                "gold_payments": [
                    {"lot_id": stock_lot["id"], "grams": "100.000", "karat": "K21"}
                ],
                "notes": "should reject",
            },
        )
        if resp.status_code == 422:
            r.ok("gold overpayment (100 > 5) rejected (422)")
        else:
            r.fail("gold overpay", f"expected 422, got {resp.status_code}")

        # Karat mismatch on gold payment
        resp = await c.post(
            f"/api/suppliers/{sup['id']}/payments",
            headers=H,
            json={
                "unit": "GOLD",
                "karat": "K24",  # we owe K21, not K24
                "amount": "1",
                "gold_payments": [
                    {"lot_id": stock_lot["id"], "grams": "1", "karat": "K24"}
                ],
                "notes": "should reject (lot is K21)",
            },
        )
        if resp.status_code == 422:
            r.ok("karat-mismatched gold repay rejected (422)")
        else:
            r.fail("karat mismatch", f"got {resp.status_code}")

        # ── Deactivation guard ────────────────────────────────────────────────
        print("\n▶ Deactivation guard")
        resp = await c.patch(
            f"/api/suppliers/{sup['id']}",
            headers=H,
            json={"is_active": False},
        )
        if resp.status_code == 409:
            r.ok("cannot deactivate supplier with outstanding debt (409)")
        else:
            r.fail("deactivate w/ debt", f"expected 409, got {resp.status_code}")

        # ── Accounts payable view ─────────────────────────────────────────────
        print("\n▶ Accounts payable")
        resp = await c.get("/api/accounts-payable", headers=H)
        ap = resp.json()
        if Decimal(ap["total_cash_owed"]) >= Decimal("400"):
            r.ok(f"AP total cash owed >= $400 (got ${ap['total_cash_owed']})")
        else:
            r.fail("AP cash total", str(ap))
        if Decimal(ap["total_grams_owed_by_karat"].get("K21", "0")) >= Decimal("5"):
            r.ok(f"AP total K21 owed >= 5g (got {ap['total_grams_owed_by_karat'].get('K21')})")
        else:
            r.fail("AP K21 total", str(ap))
        smoke_in_ap = any(s["supplier_id"] == sup["id"] for s in ap["suppliers"])
        if smoke_in_ap:
            r.ok("smoke supplier appears in AP rollup")
        else:
            r.fail("AP supplier listing", "smoke supplier missing")

        # ── Settle remaining cash + gold, then deactivate ─────────────────────
        print("\n▶ Settle and deactivate")
        resp = await c.post(
            f"/api/suppliers/{sup['id']}/payments",
            headers=H,
            json={"unit": "CASH", "amount": "400", "notes": "smoke settle cash"},
        )
        ok1 = resp.status_code == 201
        resp = await c.post(
            f"/api/suppliers/{sup['id']}/payments",
            headers=H,
            json={
                "unit": "GOLD",
                "karat": "K21",
                "amount": "5",
                "gold_payments": [
                    {"lot_id": stock_lot["id"], "grams": "5.000", "karat": "K21"}
                ],
                "notes": "smoke settle gold",
            },
        )
        ok2 = resp.status_code == 201
        if ok1 and ok2:
            r.ok("settled cash + gold to zero")
        else:
            r.fail("settle", f"cash ok={ok1}, gold ok={ok2}")

        resp = await c.patch(
            f"/api/suppliers/{sup['id']}",
            headers=H,
            json={"is_active": False},
        )
        if resp.status_code == 200:
            r.ok("deactivation now allowed (balance is zero)")
        else:
            r.fail("deactivate after settle", f"{resp.status_code} {resp.text}")

        # ── Ledger sanity ─────────────────────────────────────────────────────
        print("\n▶ Ledger")
        resp = await c.get("/api/ledger", headers=H, params={"page_size": 50})
        types = sorted({e["event_type"] for e in resp.json()["items"]})
        expected = {
            "SUPPLIER_CREATED", "SUPPLIER_PURCHASE",
            "SUPPLIER_PAYMENT_CASH", "SUPPLIER_PAYMENT_GOLD",
            "SUPPLIER_BALANCE_CHANGED", "LOT_CONSUMED", "LOT_CREATED",
        }
        if expected.issubset(set(types)):
            r.ok(f"all phase 5 events present in ledger")
        else:
            r.fail("phase 5 events", f"missing: {expected - set(types)}")

    return r.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
