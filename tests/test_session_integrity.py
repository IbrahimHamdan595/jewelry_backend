"""Session invalidation via `users.token_version` (NEX-54).

A JWT used to be valid for its whole 8-hour life no matter what happened to
the account: changing the password replaced the hash and ended nothing. Each
token now carries the user's `token_version`, `get_current_user` compares it
on every request, and bumping the column ends every session at once.

These tests go through the real routes with the real `get_current_user` —
only the database is swapped — because the thing under test is the check in
app/deps.py and the three places that bump the version.

A "device" is an HTTP client with its own cookie jar: the shop till, a phone.
"""
import asyncio

import bcrypt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.rate_limit import limiter
from app.core.security import create_access_token, decode_token
from app.deps import AUTH_COOKIE_NAME
from app.models import AuthAuditLog, InventoryLedger, Role, User

PASSWORD = "correct-horse-battery"
NEW_PASSWORD = "an-entirely-new-password"
OWNER = "owner@example.com"
CASHIER = "cashier@example.com"
ACCOUNTANT = "accountant@example.com"


def _fast_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()


async def _settle() -> None:
    """Let the fire-and-forget audit writes finish."""
    pending = [
        t for t in asyncio.all_tasks()
        if t is not asyncio.current_task()
        and getattr(t.get_coro(), "__name__", "") == "record_auth_event_safe"
    ]
    await asyncio.gather(*pending)


@pytest_asyncio.fixture
async def device(db, monkeypatch):
    """Factory for logged-out devices, all talking to the same app and DB."""
    from app.deps import get_db
    from app.main import app

    hashed = _fast_hash(PASSWORD)
    db.add(User(id="u-owner", email=OWNER, name="Owner", password_hash=hashed, role=Role.ADMIN, is_active=True))
    db.add(User(id="u-cashier", email=CASHIER, name="Cashier", password_hash=hashed, role=Role.CASHIER, is_active=True))
    db.add(User(id="u-accountant", email=ACCOUNTANT, name="Accountant", password_hash=hashed, role=Role.ACCOUNTANT, is_active=True))
    await db.commit()

    async def _get_db():
        yield db

    monkeypatch.setattr(
        "app.core.auth_audit.async_session_factory",
        async_sessionmaker(db.bind, expire_on_commit=False, class_=AsyncSession),
    )
    limiter.reset()
    app.dependency_overrides[get_db] = _get_db

    opened: list[AsyncClient] = []

    def _new() -> AsyncClient:
        # Each device gets its own address so the per-IP login limit stays out of the way.
        client = AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Forwarded-For": f"203.0.113.{len(opened) + 1}"},
        )
        opened.append(client)
        return client

    yield _new

    for client in opened:
        await client.aclose()
    app.dependency_overrides.clear()
    await _settle()


async def _login(client: AsyncClient, email: str, password: str = PASSWORD) -> str:
    """Log the device in (its cookie jar keeps the session) and return the token."""
    resp = await client.post("/api/auth/login", json={"email": email, "password": password})
    await _settle()
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _me(client: AsyncClient, token: str | None = None) -> int:
    """Status of GET /auth/me — with the device's cookie, or an explicit bearer token."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return (await client.get("/api/auth/me", headers=headers)).status_code


async def _version(db, user_id: str) -> int:
    return (await db.execute(select(User.token_version).where(User.id == user_id))).scalar_one()


# ── The claim ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_token_carries_the_version_and_nothing_else_new(device):
    """`role` is what the frontend middleware reads; `ver` is the only addition."""
    claims = decode_token(await _login(device(), OWNER))
    assert set(claims) == {"sub", "exp", "role", "ver"}
    assert (claims["sub"], claims["role"], claims["ver"]) == ("u-owner", "ADMIN", 0)


@pytest.mark.asyncio
async def test_token_without_the_claim_counts_as_version_zero(device, db):
    """Sessions minted before this deploy carry no `ver`. They must keep working
    (version 0 is every user's starting point) and still die on the first bump."""
    legacy = create_access_token(subject="u-cashier", extra={"role": "CASHIER"})
    assert "ver" not in decode_token(legacy)
    anyone = device()
    assert await _me(anyone, legacy) == 200

    await _login(anyone, OWNER)
    assert (await anyone.post("/api/staff/u-cashier/force-logout")).status_code == 204
    assert await _me(anyone, legacy) == 401


@pytest.mark.asyncio
async def test_token_from_a_newer_version_is_refused_too(device):
    """The comparison is equality, not >=: a version the server never issued is not a session."""
    future = create_access_token(subject="u-cashier", extra={"role": "CASHIER", "ver": 7})
    assert await _me(device(), future) == 401


# ── Password change ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_password_change_ends_every_other_session(device, db):
    till, phone = device(), device()
    old_till_token = await _login(till, CASHIER)
    old_phone_token = await _login(phone, CASHIER)
    assert await _me(phone) == 200

    resp = await till.post(
        "/api/auth/change-password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    assert await _version(db, "u-cashier") == 1

    # The other device is logged out, by cookie and by bearer token alike …
    assert await _me(phone) == 401
    assert await _me(device(), old_phone_token) == 401
    # … and so is the token this device held BEFORE the change.
    assert await _me(device(), old_till_token) == 401


@pytest.mark.asyncio
async def test_password_change_keeps_the_changer_logged_in(device):
    till = device()
    old_token = await _login(till, CASHIER)

    resp = await till.post(
        "/api/auth/change-password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert resp.status_code == 200, resp.text

    # A fresh cookie came back in the same response, so the till never notices.
    assert AUTH_COOKIE_NAME in resp.cookies
    assert await _me(till) == 200

    # The same token is in the body for clients that use the Authorization header.
    body = resp.json()
    assert body["access_token"] != old_token
    assert body["access_token"] == resp.cookies[AUTH_COOKIE_NAME]
    assert body["user"]["email"] == CASHIER
    assert decode_token(body["access_token"])["ver"] == 1
    assert await _me(device(), body["access_token"]) == 200

    # And the new password is the one that logs in from now on.
    assert (await device().post("/api/auth/login", json={"email": CASHIER, "password": PASSWORD})).status_code == 401
    await _login(device(), CASHIER, NEW_PASSWORD)


@pytest.mark.asyncio
async def test_wrong_current_password_changes_nothing(device, db):
    till, phone = device(), device()
    await _login(till, CASHIER)
    await _login(phone, CASHIER)

    resp = await till.post(
        "/api/auth/change-password",
        json={"current_password": "not-the-password", "new_password": NEW_PASSWORD},
    )
    assert resp.status_code == 400
    assert await _version(db, "u-cashier") == 0
    assert await _me(till) == 200
    assert await _me(phone) == 200


# ── Logout ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_logout_does_not_end_other_sessions(device, db):
    """Shop terminals may share one account: logging one till out must not
    log out the till next to it."""
    till_one, till_two = device(), device()
    await _login(till_one, CASHIER)
    await _login(till_two, CASHIER)

    assert (await till_one.post("/api/auth/logout")).status_code == 204
    await _settle()

    assert await _version(db, "u-cashier") == 0
    assert await _me(till_one) == 401   # its cookie is gone
    assert await _me(till_two) == 200


# ── Admin force-logout ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_can_force_logout_a_staff_member(device, db):
    admin, till, phone = device(), device(), device()
    await _login(admin, OWNER)
    await _login(till, CASHIER)
    await _login(phone, CASHIER)

    resp = await admin.post("/api/staff/u-cashier/force-logout")
    assert resp.status_code == 204

    assert await _me(till) == 401
    assert await _me(phone) == 401
    assert await _me(admin) == 200          # only the target is affected
    await _login(till, CASHIER)             # not a ban: they can sign back in
    assert await _me(till) == 200

    # Audited in the ledger like every other staff mutation, with the actor.
    row = (await db.execute(
        select(InventoryLedger).where(InventoryLedger.event_type == "STAFF_FORCE_LOGOUT")
    )).scalar_one()
    assert (row.actor_user_id, row.ref_type, row.ref_id) == ("u-owner", "user", "u-cashier")
    assert row.payload == {"email": CASHIER, "token_version": {"from": 0, "to": 1}}


@pytest.mark.asyncio
async def test_force_logout_reaches_every_role(device):
    """A stolen accountant session has to be revocable too, although the rest
    of the staff router only manages cashiers."""
    admin, accountant = device(), device()
    await _login(admin, OWNER)
    await _login(accountant, ACCOUNTANT)

    assert (await admin.post("/api/staff/u-accountant/force-logout")).status_code == 204
    assert await _me(accountant) == 401


@pytest.mark.asyncio
async def test_force_logout_is_admin_only(device, db):
    till, phone = device(), device()
    await _login(till, CASHIER)
    await _login(phone, ACCOUNTANT)

    assert (await till.post("/api/staff/u-owner/force-logout")).status_code == 403
    assert (await phone.post("/api/staff/u-cashier/force-logout")).status_code == 403
    assert (await device().post("/api/staff/u-cashier/force-logout")).status_code == 401
    assert await _version(db, "u-owner") == 0
    assert await _version(db, "u-cashier") == 0


@pytest.mark.asyncio
async def test_force_logout_of_an_unknown_user_is_404(device):
    admin = device()
    await _login(admin, OWNER)
    assert (await admin.post("/api/staff/no-such-user/force-logout")).status_code == 404


# ── Admin password reset ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_password_reset_ends_the_staff_members_sessions(device, db):
    """Resetting a cashier's password is a password change like any other:
    whoever was signed in with the old one is out."""
    admin, till = device(), device()
    await _login(admin, OWNER)
    await _login(till, CASHIER)

    resp = await admin.patch("/api/staff/u-cashier", json={"password": NEW_PASSWORD})
    assert resp.status_code == 200, resp.text
    assert await _version(db, "u-cashier") == 1
    assert await _me(till) == 401
    await _login(till, CASHIER, NEW_PASSWORD)


@pytest.mark.asyncio
async def test_staff_update_without_a_password_keeps_sessions(device, db):
    admin, till = device(), device()
    await _login(admin, OWNER)
    await _login(till, CASHIER)

    assert (await admin.patch("/api/staff/u-cashier", json={"name": "Renamed"})).status_code == 200
    assert await _version(db, "u-cashier") == 0
    assert await _me(till) == 200


# ── Audit ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_password_change_is_still_audited(device, db):
    till = device()
    await _login(till, CASHIER)
    await till.post("/api/auth/change-password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    await _settle()

    rows = (await db.execute(
        select(AuthAuditLog).where(AuthAuditLog.event_type == "PASSWORD_CHANGED")
    )).scalars().all()
    assert [(r.user_id, r.claimed_email) for r in rows] == [("u-cashier", CASHIER)]
