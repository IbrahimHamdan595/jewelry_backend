"""Phase 6 smoke test: melt + polish-and-relist."""

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


async def _create_used_buyback(c, H) -> dict:
    resp = await c.post(
        "/api/buybacks",
        headers=H,
        json={
            "seller_name": "Smoke Phase6 Customer",
            "seller_phone": "+961-00-666666",
            "kind": "USED_PRODUCT",
            "karat": "K18",
            "weight_grams": "4",
            "manual_price": "200",
            "notes": "smoke phase 6 used buyback",
        },
    )
    if resp.status_code != 201:
        raise RuntimeError(f"buyback setup failed: {resp.status_code} {resp.text}")
    return resp.json()


async def main() -> int:
    r = Reporter()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        if resp.status_code != 200:
            r.fail("login", resp.text)
            return r.summary()
        H = {"Authorization": f"Bearer {resp.json()['access_token']}"}

        # ── Setup: create a fresh AVAILABLE product to melt ───────────────────
        print("\n▶ Setup")
        resp = await c.post(
            "/api/products",
            headers=H,
            json={
                "name_en": "Smoke Phase6 Bracelet",
                "name_ar": "سوار",
                "category": "Bracelets",
                "karat": "K21",
                "weight_grams": "12",
                "margin_percent": "18",
                "making_charge": "30",
                "photos": [],
            },
        )
        if resp.status_code != 201:
            r.fail("setup product", resp.text)
            return r.summary()
        product = resp.json()
        r.ok(f"created product {product['code']}")

        # ── Melt a product ────────────────────────────────────────────────────
        print("\n▶ Melt product")
        resp = await c.post(
            "/api/melts",
            headers=H,
            json={"product_id": product["id"], "notes": "smoke phase6 melt"},
        )
        if resp.status_code != 201:
            r.fail("melt product", f"{resp.status_code} {resp.text}")
            return r.summary()
        melt = resp.json()
        r.ok(f"POST /melts (lot id={melt['lot']['id'][:8]}…, source_type={melt['source_type']})")

        if melt["lot"]["source"] == "MELT":
            r.ok("new lot source=MELT")
        else:
            r.fail("lot source", melt["lot"]["source"])

        if Decimal(melt["lot"]["weight_grams"]) == Decimal("12"):
            r.ok("lot weight = product weight (12g)")
        else:
            r.fail("lot weight", melt["lot"]["weight_grams"])

        # Product status flipped
        resp = await c.get(f"/api/products/{product['id']}", headers=H)
        if resp.json()["status"] == "MELTED":
            r.ok("product status → MELTED")
        else:
            r.fail("product status", resp.json()["status"])

        # Melting an already-melted product should fail
        resp = await c.post("/api/melts", headers=H, json={"product_id": product["id"]})
        if resp.status_code == 409:
            r.ok("re-melting MELTED product blocked (409)")
        else:
            r.fail("re-melt", f"expected 409, got {resp.status_code}")

        # ── Polish a USED_PRODUCT buyback ─────────────────────────────────────
        print("\n▶ Polish")
        bb = await _create_used_buyback(c, H)
        r.ok(f"created USED_PRODUCT buyback (id={bb['id'][:8]}…)")

        resp = await c.post(
            "/api/polish",
            headers=H,
            json={
                "walkin_buyback_id": bb["id"],
                "name_en": "Polished Used Ring",
                "name_ar": "خاتم مستعمل",
                "category": "Rings",
                "margin_percent": "20",
                "making_charge": "15",
                "photos": [],
                "notes": "smoke phase6 polish",
            },
        )
        if resp.status_code != 201:
            r.fail("polish", f"{resp.status_code} {resp.text}")
            return r.summary()
        polish = resp.json()
        polished_product = polish["product"]
        r.ok(f"POST /polish → product {polished_product['code']}")

        if polished_product["is_used"] is True:
            r.ok("polished product is_used=true")
        else:
            r.fail("is_used", polished_product["is_used"])
        if Decimal(polished_product["cost_basis_usd"]) == Decimal("200"):
            r.ok("cost_basis_usd carried from buyback ($200)")
        else:
            r.fail("cost basis", polished_product["cost_basis_usd"])
        if polished_product["source_ref_type"] == "walkin_buyback":
            r.ok(f"source_ref points back to buyback")
        else:
            r.fail("source_ref", polished_product["source_ref_type"])

        # Polishing the same buyback twice should fail
        resp = await c.post(
            "/api/polish",
            headers=H,
            json={
                "walkin_buyback_id": bb["id"],
                "name_en": "x",
                "category": "x",
                "margin_percent": "0",
                "making_charge": "0",
            },
        )
        if resp.status_code == 409:
            r.ok("re-polishing same buyback blocked (409)")
        else:
            r.fail("re-polish", f"expected 409, got {resp.status_code}")

        # Melting an already-polished buyback should fail
        resp = await c.post(
            "/api/melts", headers=H, json={"walkin_buyback_id": bb["id"]}
        )
        if resp.status_code == 409:
            r.ok("melting polished buyback blocked (409)")
        else:
            r.fail("melt polished", f"expected 409, got {resp.status_code}")

        # ── Melt a USED_PRODUCT buyback (different one) ───────────────────────
        print("\n▶ Melt used buyback")
        bb2 = await _create_used_buyback(c, H)
        resp = await c.post(
            "/api/melts",
            headers=H,
            json={
                "walkin_buyback_id": bb2["id"],
                "override_weight_grams": "3.8",  # weighed differently on scale
                "notes": "smoke phase6 melt used buyback",
            },
        )
        if resp.status_code != 201:
            r.fail("melt used buyback", f"{resp.status_code} {resp.text}")
        else:
            mlt = resp.json()
            r.ok(f"melted buyback → new lot {mlt['lot']['id'][:8]}…")
            if Decimal(mlt["lot"]["weight_grams"]) == Decimal("3.800"):
                r.ok("override_weight_grams honored (3.8 instead of 4)")
            else:
                r.fail("override weight", mlt["lot"]["weight_grams"])
            if Decimal(mlt["lot"]["cost_basis_usd"]) == Decimal("200"):
                r.ok("cost basis carried from buyback ($200)")
            else:
                r.fail("cost basis", mlt["lot"]["cost_basis_usd"])

        # Polishing an already-melted buyback should fail
        resp = await c.post(
            "/api/polish",
            headers=H,
            json={
                "walkin_buyback_id": bb2["id"],
                "name_en": "x",
                "category": "x",
                "margin_percent": "0",
                "making_charge": "0",
            },
        )
        if resp.status_code == 409:
            r.ok("polishing melted buyback blocked (409)")
        else:
            r.fail("polish melted", f"expected 409, got {resp.status_code}")

        # ── Argument validation ──────────────────────────────────────────────
        print("\n▶ Validation")
        resp = await c.post("/api/melts", headers=H, json={})
        if resp.status_code == 422:
            r.ok("melts: neither product_id nor buyback_id → 422")
        else:
            r.fail("melts both empty", f"got {resp.status_code}")

        resp = await c.post(
            "/api/melts",
            headers=H,
            json={"product_id": product["id"], "walkin_buyback_id": bb["id"]},
        )
        if resp.status_code == 422:
            r.ok("melts: both fields set → 422")
        else:
            r.fail("melts both set", f"got {resp.status_code}")

        # Polishing a non-USED_PRODUCT buyback should 409
        # Create a PURE_GOLD buyback first
        resp = await c.post(
            "/api/buybacks",
            headers=H,
            json={
                "seller_name": "x",
                "seller_phone": "x",
                "kind": "PURE_GOLD",
                "karat": "K18",
                "weight_grams": "1",
            },
        )
        if resp.status_code == 201:
            pure_bb_id = resp.json()["id"]
            resp = await c.post(
                "/api/polish",
                headers=H,
                json={
                    "walkin_buyback_id": pure_bb_id,
                    "name_en": "x",
                    "category": "x",
                    "margin_percent": "0",
                    "making_charge": "0",
                },
            )
            if resp.status_code == 409:
                r.ok("polishing PURE_GOLD buyback blocked (409, wrong kind)")
            else:
                r.fail("polish wrong kind", f"got {resp.status_code}")

        # ── Ledger ────────────────────────────────────────────────────────────
        print("\n▶ Ledger")
        resp = await c.get("/api/ledger", headers=H, params={"page_size": 50})
        types = {e["event_type"] for e in resp.json()["items"]}
        if "MELT" in types and "POLISH" in types:
            r.ok("MELT and POLISH events visible in ledger")
        else:
            r.fail("ledger events", f"types include: {sorted(types)[:8]}")

    return r.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
