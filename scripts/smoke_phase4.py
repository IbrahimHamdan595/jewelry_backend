"""Phase 4 smoke test: product extensions + sell coins/ounces + void reversal.

Run:
    cd jewelry_backend && ./.venv/bin/python -m scripts.smoke_phase4
"""

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
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def ok(self, label: str) -> None:
        print(f"  ✓ {label}")
        self.passed.append(label)

    def fail(self, label: str, detail: str) -> None:
        print(f"  ✗ {label}\n      {detail}")
        self.failed.append((label, detail))

    def summary(self) -> int:
        total = len(self.passed) + len(self.failed)
        print(f"\n  {len(self.passed)}/{total} passed, {len(self.failed)} failed")
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

        # ── Product schema extensions ─────────────────────────────────────────
        print("\n▶ Product schema extensions")

        # Find any existing product to inspect new fields
        resp = await c.get("/api/products", headers=H)
        items = resp.json().get("items", [])
        if items:
            p = items[0]
            need = {"is_used", "cost_basis_usd", "status", "source_ref_type", "source_ref_id"}
            missing = need - set(p.keys())
            if not missing:
                r.ok(f"ProductOut returns new fields; sample status={p['status']}, is_used={p['is_used']}")
            else:
                r.fail("ProductOut fields", f"missing: {missing}")
        else:
            r.ok("no products yet — schema check skipped")

        # Create a fresh product so we can sell it
        resp = await c.post(
            "/api/products",
            headers=H,
            json={
                "name_en": "Smoke Phase4 Ring",
                "name_ar": "خاتم",
                "category": "Rings",
                "karat": "K21",
                "weight_grams": "5",
                "margin_percent": "20",
                "making_charge": "25",
                "photos": [],
            },
        )
        if resp.status_code != 201:
            r.fail("create product", f"{resp.status_code} {resp.text}")
            return r.summary()
        product = resp.json()
        if product.get("status") == "AVAILABLE":
            r.ok(f"new product status=AVAILABLE (id={product['id'][:8]}…, code={product['code']})")
        else:
            r.fail("new product status", f"expected AVAILABLE, got {product.get('status')}")

        # Find existing coin/ounce types for cart
        resp = await c.get("/api/coins", headers=H, params={"is_active": True})
        coins = resp.json().get("items", [])
        coin = next((co for co in coins if co["on_hand_qty"] > 0), None)
        if not coin:
            r.fail("setup", "no coin with stock; run Phase 3 smoke first")
            return r.summary()

        resp = await c.get("/api/ounces", headers=H, params={"is_active": True})
        ounce = next((o for o in resp.json().get("items", []) if o["on_hand_qty"] >= 0), None)
        # Top-up ounce stock to ensure we have at least 1
        if ounce and ounce["on_hand_qty"] < 1:
            await c.post(
                "/api/adjustments",
                headers=H,
                json={
                    "target_type": "OUNCE_STOCK",
                    "target_id": ounce["id"],
                    "delta": "1",
                    "reason": "CORRECTION",
                    "notes": "smoke phase4 top-up",
                },
            )
            ounce["on_hand_qty"] = 1

        # ── Mixed-cart checkout ───────────────────────────────────────────────
        print("\n▶ Mixed-cart checkout (PRODUCT + COIN + OUNCE)")

        cart_items = [
            {"item_kind": "PRODUCT", "product_id": product["id"], "quantity": 1},
            {"item_kind": "COIN", "coin_type_id": coin["id"], "quantity": 2},
        ]
        if ounce and ounce["on_hand_qty"] >= 1:
            cart_items.append({"item_kind": "OUNCE", "ounce_type_id": ounce["id"], "quantity": 1})

        coin_before = coin["on_hand_qty"]
        ounce_before = ounce["on_hand_qty"] if ounce else None

        resp = await c.post(
            "/api/orders",
            headers=H,
            json={
                "items": cart_items,
                "payment_method": "CASH",
                "customer_name": "Smoke Phase4 Customer",
            },
        )
        if resp.status_code != 201:
            r.fail("mixed checkout", f"{resp.status_code} {resp.text}")
            return r.summary()
        order = resp.json()
        r.ok(f"order created ({order['order_number']}, total ${order['total_usd']}, items={len(order['items'])})")

        # Inspect item_kind on each line
        kinds = sorted({it["item_kind"] for it in order["items"]})
        if "PRODUCT" in kinds and "COIN" in kinds:
            r.ok(f"order lines carry item_kind: {kinds}")
        else:
            r.fail("item_kind discrim", f"got kinds={kinds}")

        # Product status flipped to SOLD
        resp = await c.get(f"/api/products/{product['id']}", headers=H)
        new_status = resp.json().get("status")
        if new_status == "SOLD":
            r.ok("product status → SOLD")
        else:
            r.fail("product status after sale", f"expected SOLD, got {new_status}")

        # Coin stock decremented
        resp = await c.get(f"/api/coins/{coin['id']}", headers=H)
        coin_after = resp.json()["on_hand_qty"]
        if coin_after == coin_before - 2:
            r.ok(f"coin on_hand_qty decremented: {coin_before} → {coin_after}")
        else:
            r.fail("coin stock", f"expected {coin_before - 2}, got {coin_after}")

        if ounce and ounce_before is not None:
            resp = await c.get(f"/api/ounces/{ounce['id']}", headers=H)
            ounce_after = resp.json()["on_hand_qty"]
            if ounce_after == ounce_before - 1:
                r.ok(f"ounce on_hand_qty decremented: {ounce_before} → {ounce_after}")
            else:
                r.fail("ounce stock", f"expected {ounce_before - 1}, got {ounce_after}")

        # ── Reselling SOLD product blocked ────────────────────────────────────
        print("\n▶ Sale guards")
        resp = await c.post(
            "/api/orders",
            headers=H,
            json={
                "items": [{"item_kind": "PRODUCT", "product_id": product["id"], "quantity": 1}],
                "payment_method": "CASH",
            },
        )
        if resp.status_code == 409:
            r.ok("reselling SOLD product correctly blocked (409)")
        else:
            r.fail("resell SOLD", f"expected 409, got {resp.status_code}")

        # Coin overdraw blocked
        resp = await c.post(
            "/api/orders",
            headers=H,
            json={
                "items": [{"item_kind": "COIN", "coin_type_id": coin["id"], "quantity": 9999}],
                "payment_method": "CASH",
            },
        )
        if resp.status_code == 409:
            r.ok("coin overdraw blocked (409)")
        else:
            r.fail("coin overdraw", f"expected 409, got {resp.status_code}")

        # Quantity cap
        resp = await c.post(
            "/api/orders",
            headers=H,
            json={
                "items": [{"item_kind": "COIN", "coin_type_id": coin["id"], "quantity": 200}],
                "payment_method": "CASH",
            },
        )
        if resp.status_code == 422:
            r.ok("quantity > 100/line capped (422)")
        else:
            r.fail("qty cap", f"expected 422, got {resp.status_code}")

        # ── Void reverses stock ───────────────────────────────────────────────
        print("\n▶ Void reversal")
        resp = await c.post(
            f"/api/orders/{order['id']}/void",
            headers=H,
            json={"reason": "smoke phase4 void"},
        )
        if resp.status_code != 200:
            r.fail("void", f"{resp.status_code} {resp.text}")
        else:
            r.ok("void succeeded")
            resp = await c.get(f"/api/products/{product['id']}", headers=H)
            if resp.json()["status"] == "AVAILABLE":
                r.ok("product status reverted SOLD → AVAILABLE on void")
            else:
                r.fail("product revert", f"got {resp.json()['status']}")
            resp = await c.get(f"/api/coins/{coin['id']}", headers=H)
            qty = resp.json()["on_hand_qty"]
            if qty == coin_before:
                r.ok(f"coin stock restored: {coin_after} → {qty}")
            else:
                r.fail("coin restore", f"expected {coin_before}, got {qty}")

        # ── PRODUCT adjustment ────────────────────────────────────────────────
        print("\n▶ PRODUCT manual adjustment")
        resp = await c.post(
            "/api/adjustments",
            headers=H,
            json={
                "target_type": "PRODUCT",
                "target_id": product["id"],
                "delta": "-1",
                "reason": "LOSS",
                "notes": "smoke phase4 product lost",
            },
        )
        if resp.status_code == 201:
            r.ok("PRODUCT adjustment delta=-1 accepted")
            resp = await c.get(f"/api/products/{product['id']}", headers=H)
            if resp.json()["status"] == "INACTIVE":
                r.ok("product → INACTIVE after delta=-1")
            else:
                r.fail("product status after -1", resp.json()["status"])
        else:
            r.fail("product adj -1", f"{resp.status_code} {resp.text}")

        # Restore
        resp = await c.post(
            "/api/adjustments",
            headers=H,
            json={
                "target_type": "PRODUCT",
                "target_id": product["id"],
                "delta": "1",
                "reason": "CORRECTION",
                "notes": "smoke phase4 product restored",
            },
        )
        if resp.status_code == 201:
            resp = await c.get(f"/api/products/{product['id']}", headers=H)
            if resp.json()["status"] == "AVAILABLE":
                r.ok("product → AVAILABLE after delta=+1")
            else:
                r.fail("product status after +1", resp.json()["status"])
        else:
            r.fail("product adj +1", f"{resp.status_code} {resp.text}")

        # Invalid delta
        resp = await c.post(
            "/api/adjustments",
            headers=H,
            json={
                "target_type": "PRODUCT",
                "target_id": product["id"],
                "delta": "-3",
                "reason": "LOSS",
                "notes": "should fail",
            },
        )
        if resp.status_code == 422:
            r.ok("PRODUCT non ±1 delta rejected (422)")
        else:
            r.fail("product delta -3", f"expected 422, got {resp.status_code}")

        # ── Ledger spot-check ────────────────────────────────────────────────
        print("\n▶ Ledger")
        resp = await c.get("/api/ledger", headers=H, params={"page_size": 30})
        types = sorted({e["event_type"] for e in resp.json()["items"]})
        expected = {"SALE_PRODUCT", "SALE_COIN", "ORDER_VOID", "PRODUCT_STATUS_CHANGED"}
        present = expected & set(types)
        if expected.issubset(set(types)):
            r.ok(f"all phase 4 events visible in ledger: {sorted(expected)}")
        else:
            r.fail("phase 4 events", f"missing: {expected - set(types)}; present: {present}")

    return r.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
