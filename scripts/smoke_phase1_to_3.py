"""End-to-end smoke test for Phases 1–3 against the live (Neon) DB.

Hits the FastAPI app via httpx ASGI transport. Logs in as the seeded admin,
exercises every endpoint added in Phases 1, 2, and 3, and prints a
pass/fail summary.

Run:
    cd jewelry_backend && ./.venv/bin/python -m scripts.smoke_phase1_to_3
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
        print()
        print(f"  {len(self.passed)}/{total} passed, {len(self.failed)} failed")
        if self.failed:
            print("\n  Failures:")
            for label, detail in self.failed:
                print(f"    - {label}: {detail}")
        return 0 if not self.failed else 1


async def main() -> int:
    r = Reporter()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # ── Auth ──────────────────────────────────────────────────────────────
        print("\n▶ Auth")
        resp = await c.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        if resp.status_code != 200:
            r.fail("login", f"{resp.status_code} {resp.text}")
            return r.summary()
        token = resp.json()["access_token"]
        r.ok(f"admin login (status 200)")
        H = {"Authorization": f"Bearer {token}"}

        # ── Phase 1: pure gold lots + ledger ──────────────────────────────────
        print("\n▶ Phase 1 — Pure gold lots + audit ledger")

        resp = await c.post(
            "/api/lots",
            headers=H,
            json={
                "karat": "K21",
                "weight_grams": "100",
                "source": "SEED",
                "cost_basis_usd": "7500",
                "notes": "smoke: opening K21 lot",
            },
        )
        if resp.status_code != 201:
            r.fail("create K21 lot", f"{resp.status_code} {resp.text}")
            return r.summary()
        lot_k21 = resp.json()
        r.ok(f"POST /lots K21 100g (id={lot_k21['id'][:8]}…)")

        resp = await c.post(
            "/api/lots",
            headers=H,
            json={"karat": "K22", "weight_grams": "50", "source": "SEED", "cost_basis_usd": "4000"},
        )
        if resp.status_code == 201:
            r.ok("POST /lots K22 (Phase 3 enum proves out)")
            lot_k22 = resp.json()
        else:
            r.fail("create K22 lot", f"{resp.status_code} {resp.text}")
            lot_k22 = None

        resp = await c.get("/api/lots/totals", headers=H)
        totals = resp.json() if resp.status_code == 200 else {}
        k21_remaining = next(
            (Decimal(k["total_remaining_grams"]) for k in totals.get("by_karat", []) if k["karat"] == "K21"),
            None,
        )
        if k21_remaining is not None and k21_remaining >= Decimal("100"):
            r.ok(f"GET /lots/totals shows K21 ≥ 100g (got {k21_remaining})")
        else:
            r.fail("totals K21", f"expected ≥100g, got {k21_remaining}")

        # Adjustment: lose 2.5g
        resp = await c.post(
            "/api/adjustments",
            headers=H,
            json={
                "target_type": "LOT",
                "target_id": lot_k21["id"],
                "delta": "-2.5",
                "reason": "LOSS",
                "notes": "smoke: scale recalibration",
            },
        )
        if resp.status_code == 201:
            r.ok("POST /adjustments LOT -2.5g LOSS")
        else:
            r.fail("lot adjustment", f"{resp.status_code} {resp.text}")

        # Overdraw should fail
        resp = await c.post(
            "/api/adjustments",
            headers=H,
            json={
                "target_type": "LOT",
                "target_id": lot_k21["id"],
                "delta": "-9999",
                "reason": "CORRECTION",
                "notes": "smoke: should fail",
            },
        )
        if resp.status_code == 422:
            r.ok("overdraw adjustment correctly rejected (422)")
        else:
            r.fail("overdraw should fail", f"got {resp.status_code} {resp.text[:120]}")

        # Ledger should now have entries
        resp = await c.get("/api/ledger", headers=H, params={"ref_type": "gold_lot", "ref_id": lot_k21["id"]})
        if resp.status_code == 200:
            count = resp.json()["total"]
            if count >= 2:
                r.ok(f"GET /ledger shows ≥2 events for K21 lot (got {count})")
            else:
                r.fail("ledger count", f"expected ≥2, got {count}")
        else:
            r.fail("ledger query", f"{resp.status_code} {resp.text}")

        # ── Phase 2: coin & ounce catalogs ────────────────────────────────────
        print("\n▶ Phase 2 — Coin & Ounce catalogs + stock")

        resp = await c.post(
            "/api/coins",
            headers=H,
            json={
                "code": "SMOKE-OTTOMAN-1",
                "name_en": "Ottoman Lira (smoke)",
                "name_ar": "ليرة عثمانية",
                "karat": "K22",  # Real Ottoman lira is 22K — tests K22
                "weight_grams": "7.216",
                "markup_per_gram": "3.5",
                "margin_mode": "USD",
                "margin_value": "15",
                "min_stock_qty": 5,
            },
        )
        if resp.status_code == 201:
            coin = resp.json()
            r.ok(f"POST /coins K22 coin created (id={coin['id'][:8]}…)")
        else:
            r.fail("create coin type", f"{resp.status_code} {resp.text}")
            coin = None

        if coin:
            resp = await c.get(f"/api/coins/{coin['id']}/price", headers=H)
            if resp.status_code == 200:
                price = resp.json()
                expected_metal = Decimal(price["effective_rate"]) * Decimal("7.216")
                expected_final = expected_metal + Decimal("15")
                actual_final = Decimal(price["final_price"])
                if abs(actual_final - expected_final) < Decimal("0.05"):
                    r.ok(f"GET /coins/{{id}}/price = ${actual_final} (formula matches)")
                else:
                    r.fail("coin price formula", f"expected ~{expected_final}, got {actual_final}")
            else:
                r.fail("coin price", f"{resp.status_code} {resp.text}")

            # Stock adjustment +10
            resp = await c.post(
                "/api/adjustments",
                headers=H,
                json={
                    "target_type": "COIN_STOCK",
                    "target_id": coin["id"],
                    "delta": "10",
                    "reason": "CORRECTION",
                    "notes": "smoke: opening count",
                },
            )
            if resp.status_code == 201:
                r.ok("POST /adjustments COIN_STOCK +10")
            else:
                r.fail("coin stock adjustment", f"{resp.status_code} {resp.text}")

            resp = await c.get(f"/api/coins/{coin['id']}", headers=H)
            qty = resp.json().get("on_hand_qty") if resp.status_code == 200 else None
            if qty == 10:
                r.ok(f"coin on_hand_qty == 10")
            else:
                r.fail("coin qty after adj", f"expected 10, got {qty}")

            # Fractional delta should fail for COIN_STOCK
            resp = await c.post(
                "/api/adjustments",
                headers=H,
                json={
                    "target_type": "COIN_STOCK",
                    "target_id": coin["id"],
                    "delta": "1.5",
                    "reason": "CORRECTION",
                    "notes": "smoke: should fail",
                },
            )
            if resp.status_code == 422:
                r.ok("fractional COIN_STOCK delta rejected (422)")
            else:
                r.fail("fractional coin delta", f"expected 422, got {resp.status_code}")

        # Ounce type
        resp = await c.post(
            "/api/ounces",
            headers=H,
            json={
                "code": "SMOKE-1OZ-K24",
                "name_en": "1 oz Bar (smoke)",
                "karat": "K24",
                "weight_grams": "31.1035",
                "markup_per_gram": "1.0",
                "margin_mode": "PERCENT",
                "margin_value": "2",
                "min_stock_qty": 2,
            },
        )
        if resp.status_code == 201:
            ounce = resp.json()
            r.ok(f"POST /ounces 1oz K24 (id={ounce['id'][:8]}…)")
            resp = await c.get(f"/api/ounces/{ounce['id']}/price", headers=H)
            if resp.status_code == 200:
                r.ok(f"GET /ounces/{{id}}/price → ${resp.json()['final_price']}")
            else:
                r.fail("ounce price", f"{resp.status_code}")
        else:
            r.fail("create ounce type", f"{resp.status_code} {resp.text}")
            ounce = None

        # ── Phase 3: walk-in buybacks ─────────────────────────────────────────
        print("\n▶ Phase 3 — Walk-in buybacks")

        # Quote
        resp = await c.get(
            "/api/buybacks/quote",
            headers=H,
            params={"karat": "K21", "weight_grams": "10"},
        )
        if resp.status_code == 200:
            quote = resp.json()
            r.ok(f"GET /buybacks/quote K21 10g → ${quote['buy_price']} (rate={quote['rate_24k']})")
        else:
            r.fail("buyback quote", f"{resp.status_code} {resp.text}")
            quote = None

        # PURE_GOLD buyback
        resp = await c.post(
            "/api/buybacks",
            headers=H,
            json={
                "seller_name": "Smoke Test Customer",
                "seller_phone": "+961-00-000000",
                "kind": "PURE_GOLD",
                "karat": "K21",
                "weight_grams": "10",
                "notes": "smoke phase 3",
            },
        )
        if resp.status_code == 201:
            bb = resp.json()
            r.ok(f"POST /buybacks PURE_GOLD 10g K21 → lot {bb.get('result_lot_id', '')[:8]}…")
        else:
            r.fail("pure_gold buyback", f"{resp.status_code} {resp.text}")
            bb = None

        # Lot totals should reflect the new buyback lot
        resp = await c.get("/api/lots/totals", headers=H)
        new_k21 = next(
            (Decimal(k["total_remaining_grams"]) for k in resp.json().get("by_karat", []) if k["karat"] == "K21"),
            Decimal("0"),
        )
        if k21_remaining is not None and new_k21 >= k21_remaining + Decimal("10") - Decimal("0.001"):
            r.ok(f"K21 pool grew by buyback (was {k21_remaining}, now {new_k21})")
        else:
            r.fail("K21 pool growth", f"was {k21_remaining}, now {new_k21}")

        # COIN buyback (if coin exists)
        if coin:
            resp = await c.post(
                "/api/buybacks",
                headers=H,
                json={
                    "seller_name": "Smoke Test Customer",
                    "seller_phone": "+961-00-000001",
                    "kind": "COIN",
                    "coin_type_id": coin["id"],
                    "quantity": 2,
                    "notes": "smoke phase 3 — coin buyback",
                },
            )
            if resp.status_code == 201:
                r.ok("POST /buybacks COIN qty=2")
                # qty should now be 12 (10 from adjustment + 2 from buyback)
                resp = await c.get(f"/api/coins/{coin['id']}", headers=H)
                qty = resp.json().get("on_hand_qty")
                if qty == 12:
                    r.ok(f"coin on_hand_qty == 12 after buyback")
                else:
                    r.fail("coin qty after buyback", f"expected 12, got {qty}")
            else:
                r.fail("coin buyback", f"{resp.status_code} {resp.text}")

        # Drift rejection
        resp = await c.post(
            "/api/buybacks",
            headers=H,
            json={
                "seller_name": "Smoke Test Customer",
                "seller_phone": "+961-00-000002",
                "kind": "PURE_GOLD",
                "karat": "K21",
                "weight_grams": "1",
                "expected_rate": "1.00",  # absurdly stale
            },
        )
        if resp.status_code == 409:
            r.ok("rate drift correctly rejected (409)")
        else:
            r.fail("drift check", f"expected 409, got {resp.status_code} {resp.text[:120]}")

        # USED_PRODUCT buyback (Phase 3 stub)
        resp = await c.post(
            "/api/buybacks",
            headers=H,
            json={
                "seller_name": "Smoke Test Customer",
                "seller_phone": "+961-00-000003",
                "kind": "USED_PRODUCT",
                "karat": "K18",
                "weight_grams": "5",
                "manual_price": "300",
                "notes": "smoke: used piece pending polish",
            },
        )
        if resp.status_code == 201:
            used = resp.json()
            r.ok(f"POST /buybacks USED_PRODUCT (id={used['id'][:8]}…, no product yet — Phase 6)")
        else:
            r.fail("used_product buyback", f"{resp.status_code} {resp.text}")

        # Ledger spot-check — should have many entries
        resp = await c.get("/api/ledger", headers=H, params={"page_size": 50})
        if resp.status_code == 200:
            events = resp.json()["items"]
            types = sorted(set(e["event_type"] for e in events))
            r.ok(f"ledger has {len(events)} recent entries, types: {types[:6]}{'…' if len(types) > 6 else ''}")
        else:
            r.fail("ledger list", f"{resp.status_code}")

    return r.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
