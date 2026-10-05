"""Login throttling (NEX-47): the per-IP rate limit and the per-account lockout.

Both layers are exercised through the real `/api/auth/login` route on the real
app, because the bugs they fix lived in the wiring: the limiter was keyed on
the load balancer's address, and nothing limited attempts per account at all.

`ip=` on the helpers sets X-Forwarded-For the way Render's ingress does
("<client>, <proxy hop>"); the socket peer is the same for every request, as it
is in production.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from starlette.requests import Request

from app.core import login_lockout
from app.core.audit_chain import verify_auth_chain
from app.core.auth_audit import EVENT_ACCOUNT_LOCKED, EVENT_ACCOUNT_UNLOCKED, EVENT_LOGIN_FAILED
from app.core.rate_limit import client_ip_key, limiter
from app.models import AuthAuditChainHead, AuthAuditLog
from tests.conftest import ACCOUNTANT_EMAIL as ACCOUNTANT
from tests.conftest import AUTH_PASSWORD as PASSWORD
from tests.conftest import CASHIER_EMAIL as CASHIER
from tests.conftest import OWNER_EMAIL as OWNER
from tests.conftest import settle


@pytest.fixture
def client(device):
    """One device is enough here: every request names its own address."""
    return device()


async def _login(client, email: str, password: str, *, ip: str):
    resp = await client.post(
        "/api/auth/login",
        json={"email": email, "password": password},
        headers={"X-Forwarded-For": f"{ip}, 10.0.0.1"},
    )
    await settle()
    return resp


# ── Rate-limit key ────────────────────────────────────────────────────────────

def _request(headers: dict, peer: tuple[str, int] | None = ("10.0.0.2", 0)) -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/api/auth/login",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "query_string": b"",
        "client": peer,
    })


def test_limiter_is_keyed_on_the_client_ip():
    assert limiter._key_func is client_ip_key


def test_key_is_the_leftmost_forwarded_entry():
    """The leftmost entry is the visitor; the rightmost is the proxy."""
    req = _request({"x-forwarded-for": "203.0.113.5, 10.0.0.1, 10.0.0.2"})
    assert client_ip_key(req) == "203.0.113.5"


def test_key_canonicalises_ipv6():
    """One address must not get a second bucket by being spelled differently."""
    a = client_ip_key(_request({"x-forwarded-for": "2001:DB8:0:0:0:0:0:1"}))
    b = client_ip_key(_request({"x-forwarded-for": "2001:db8::1"}))
    assert a == b == "2001:db8::1"


@pytest.mark.parametrize("xff", ["", "   ", ",", ", 203.0.113.5", "unknown", "not-an-ip", "203.0.113.5:4711", "203.0.113"])
def test_malformed_forwarded_header_falls_back_to_the_socket_address(xff):
    assert client_ip_key(_request({"x-forwarded-for": xff})) == "10.0.0.2"


def test_key_without_forwarded_header_is_the_socket_address():
    """Local dev: no proxy, the peer is the client."""
    assert client_ip_key(_request({}, peer=("127.0.0.1", 0))) == "127.0.0.1"


def test_key_without_header_or_peer_is_still_a_string():
    assert client_ip_key(_request({}, peer=None)) == "127.0.0.1"


@pytest.mark.asyncio
async def test_login_budget_is_per_client_ip(client):
    """Exhausting one visitor's 5/minute must not lock out the next visitor."""
    for i in range(5):
        resp = await _login(client, f"nobody{i}@example.com", "wrong-password", ip="203.0.113.5")
        assert resp.status_code == 401
    resp = await _login(client, "nobody5@example.com", "wrong-password", ip="203.0.113.5")
    assert resp.status_code == 429

    # Same load balancer, different visitor: a full budget of their own.
    resp = await _login(client, OWNER, PASSWORD, ip="198.51.100.7")
    assert resp.status_code == 200


# ── Per-account lockout ───────────────────────────────────────────────────────
#
# Every failed attempt below comes from a DIFFERENT address, which is exactly
# the case the per-IP limit cannot see: a distributed guess at one account.

async def _fail(client, email: str, times: int, *, subnet: str = "198.51.100") -> None:
    for i in range(times):
        resp = await _login(client, email, "wrong-password", ip=f"{subnet}.{i + 1}")
        assert resp.status_code == 401, resp.text


async def _events(db, event_type: str, email: str | None = None) -> list[AuthAuditLog]:
    q = select(AuthAuditLog).where(AuthAuditLog.event_type == event_type)
    if email is not None:
        q = q.where(AuthAuditLog.claimed_email == email)
    return list((await db.execute(q.order_by(AuthAuditLog.occurred_at))).scalars().all())


async def _seed_event(db, event_type: str, email: str, *, minutes_ago: float) -> None:
    """An audit row from the past. Not chained — the lockout only reads
    event_type / claimed_email / occurred_at."""
    at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    db.add(AuthAuditLog(
        id=uuid4().hex, event_type=event_type, occurred_at=at, user_id=None,
        claimed_email=email, client_ip="203.0.113.9", user_agent=None, detail=None,
        retention_until_at=at + timedelta(days=540), prev_hash="seed", entry_hash=uuid4().hex,
    ))
    await db.commit()


async def _chain(db) -> tuple[str, int, bool]:
    """(status, rows, head matches) of the auth-audit chain as it now stands."""
    rows = (await db.execute(select(AuthAuditLog).order_by(AuthAuditLog.occurred_at, AuthAuditLog.id))).scalars().all()
    result = verify_auth_chain([
        {
            "id": r.id, "prev_hash": r.prev_hash, "entry_hash": r.entry_hash,
            "event_type": r.event_type, "occurred_at": r.occurred_at, "user_id": r.user_id,
            "claimed_email": r.claimed_email, "client_ip": r.client_ip,
            "user_agent": r.user_agent, "detail": r.detail,
        }
        for r in rows
    ])
    assert result["first_break"] is None, result
    head = (await db.execute(select(AuthAuditChainHead))).scalar_one()
    await db.refresh(head)
    head_matches = (head.row_count, head.latest_entry_hash) == (len(rows), rows[-1].entry_hash)
    return result["status"], result["total_rows"], head_matches


def _advance_clock(monkeypatch, *, minutes: float) -> None:
    """Move the lockout's idea of "now" forward; audit rows keep their stamps."""
    later = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    monkeypatch.setattr(login_lockout, "_now", lambda: later)


def test_thresholds_are_the_agreed_ones():
    assert login_lockout.LOCKOUT_THRESHOLD == 10
    assert login_lockout.LOCKOUT_WINDOW == timedelta(minutes=15)
    assert login_lockout.LOCKOUT_DURATION == timedelta(minutes=15)


@pytest.mark.asyncio
async def test_ten_consecutive_failures_lock_the_account(client):
    await _fail(client, OWNER, 10)

    # Locked: even the correct password, from an address never seen, is refused.
    resp = await _login(client, OWNER, PASSWORD, ip="203.0.113.77")
    assert resp.status_code == 429
    assert resp.json() == {"detail": "Too many failed login attempts. Try again later."}
    assert "set-cookie" not in resp.headers


@pytest.mark.asyncio
async def test_lock_is_on_the_account_not_on_the_endpoint(client):
    await _fail(client, OWNER, 10)
    assert (await _login(client, OWNER, PASSWORD, ip="203.0.113.77")).status_code == 429

    resp = await _login(client, CASHIER, PASSWORD, ip="203.0.113.78")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_nine_failures_do_not_lock(client):
    await _fail(client, OWNER, 9)
    assert (await _login(client, OWNER, PASSWORD, ip="203.0.113.77")).status_code == 200


@pytest.mark.asyncio
async def test_a_successful_login_resets_the_count(client):
    """Only CONSECUTIVE failures count: 18 failures with a success in the
    middle is two ordinary bad mornings, not an attack."""
    await _fail(client, OWNER, 9)
    assert (await _login(client, OWNER, PASSWORD, ip="203.0.113.77")).status_code == 200
    await _fail(client, OWNER, 9, subnet="192.0.2")
    assert (await _login(client, OWNER, PASSWORD, ip="203.0.113.77")).status_code == 200


@pytest.mark.asyncio
async def test_unknown_email_is_locked_exactly_like_a_real_account(client):
    """No account-existence oracle: an address with no user behind it walks
    through the same 401s and hits the same 429 as a real one."""
    ghost = "ghost@example.com"
    real, fake = [], []
    for i in range(11):
        real.append(await _login(client, OWNER, "wrong-password", ip=f"198.51.100.{i + 1}"))
        fake.append(await _login(client, ghost, "wrong-password", ip=f"192.0.2.{i + 1}"))

    assert [r.status_code for r in real] == [401] * 10 + [429]
    assert [(r.status_code, r.json()) for r in fake] == [(r.status_code, r.json()) for r in real]


@pytest.mark.asyncio
async def test_lock_is_keyed_on_the_normalised_email(client):
    """Changing the capitalisation must not buy another ten guesses."""
    spellings = ["OWNER@example.com", "Owner@Example.com", "owner@EXAMPLE.COM", "oWnEr@example.com", OWNER]
    for i in range(10):
        resp = await _login(client, spellings[i % len(spellings)], "wrong-password", ip=f"198.51.100.{i + 1}")
        assert resp.status_code == 401

    assert (await _login(client, OWNER, PASSWORD, ip="203.0.113.77")).status_code == 429


@pytest.mark.asyncio
async def test_lock_expires_after_fifteen_minutes(client, monkeypatch):
    await _fail(client, OWNER, 10)
    assert (await _login(client, OWNER, PASSWORD, ip="203.0.113.77")).status_code == 429

    _advance_clock(monkeypatch, minutes=14)
    assert (await _login(client, OWNER, PASSWORD, ip="203.0.113.78")).status_code == 429

    _advance_clock(monkeypatch, minutes=15.1)
    assert (await _login(client, OWNER, PASSWORD, ip="203.0.113.79")).status_code == 200


@pytest.mark.asyncio
async def test_attempts_while_locked_do_not_extend_the_lock(client, db, monkeypatch):
    """Otherwise anyone could keep an account locked forever by hammering it."""
    await _fail(client, OWNER, 10)
    for i in range(5):
        resp = await _login(client, OWNER, "wrong-password", ip=f"192.0.2.{i + 1}")
        assert resp.status_code == 429

    assert len(await _events(db, EVENT_LOGIN_FAILED, OWNER)) == 10  # refused attempts are not failures
    _advance_clock(monkeypatch, minutes=15.1)
    assert (await _login(client, OWNER, PASSWORD, ip="203.0.113.77")).status_code == 200


@pytest.mark.asyncio
async def test_failures_older_than_the_window_do_not_count(client, db):
    for _ in range(9):
        await _seed_event(db, EVENT_LOGIN_FAILED, OWNER, minutes_ago=16)
    await _fail(client, OWNER, 9)
    assert (await _login(client, OWNER, PASSWORD, ip="203.0.113.77")).status_code == 200


@pytest.mark.asyncio
async def test_a_busy_shop_day_does_not_trip_the_lockout(client, db):
    """Eight hours of typos — a mistyped password every three minutes, never
    ten inside any fifteen — plus a few more right now."""
    for minute in range(3, 8 * 60, 3):
        await _seed_event(db, EVENT_LOGIN_FAILED, CASHIER, minutes_ago=minute)
    await _fail(client, CASHIER, 4)
    assert (await _login(client, CASHIER, PASSWORD, ip="203.0.113.77")).status_code == 200


@pytest.mark.asyncio
async def test_lockout_is_audited_once(client, db):
    await _fail(client, OWNER, 10)
    await _login(client, OWNER, PASSWORD, ip="203.0.113.77")  # refused

    locked = await _events(db, EVENT_ACCOUNT_LOCKED)
    assert len(locked) == 1
    assert locked[0].claimed_email == OWNER
    assert locked[0].user_id is None  # same row whether or not the account exists
    assert locked[0].client_ip == "198.51.100.10"  # the attempt that tipped it over
    assert len(await _events(db, EVENT_LOGIN_FAILED, OWNER)) == 10


@pytest.mark.asyncio
async def test_login_survives_an_audit_write_failure(client):
    """Best effort still holds: a broken recorder must not turn a 401 into a 500."""
    with patch("app.core.auth_audit.compute_auth_entry_hash", side_effect=ValueError("simulated chain error")):
        resp = await _login(client, OWNER, "wrong-password", ip="198.51.100.1")
    assert resp.status_code == 401

    assert (await _login(client, OWNER, PASSWORD, ip="198.51.100.2")).status_code == 200


@pytest.mark.asyncio
async def test_auth_chain_stays_intact_across_inline_and_background_writes(client, db):
    """Failed logins are written on the request's session, successes by the
    background recorder. Both must extend the same single chain."""
    await _fail(client, OWNER, 3)
    assert (await _login(client, OWNER, PASSWORD, ip="203.0.113.77")).status_code == 200
    await _fail(client, CASHIER, 10)
    assert (await _login(client, CASHIER, PASSWORD, ip="203.0.113.78")).status_code == 429

    assert await _chain(db) == ("intact", 15, True)


# ── Admin unlock ──────────────────────────────────────────────────────────────
#
# The lockout is also a way to lock someone ELSE out: ten requests against a
# known email and its owner cannot sign in for fifteen minutes, repeatably.
# An admin can release an account at once.

async def _signed_in(device, email: str):
    """A device with a live session for `email`."""
    client = device()
    assert (await _login(client, email, PASSWORD, ip="203.0.113.200")).status_code == 200
    return client


@pytest.mark.asyncio
async def test_admin_unlock_releases_the_lock_at_once(device, db):
    admin, till = await _signed_in(device, OWNER), device()
    await _fail(till, CASHIER, 10)
    assert (await _login(till, CASHIER, PASSWORD, ip="203.0.113.77")).status_code == 429

    resp = await admin.post("/api/staff/u-cashier/unlock")
    assert resp.status_code == 204

    # No waiting out the fifteen minutes: the very next attempt gets in.
    assert (await _login(till, CASHIER, PASSWORD, ip="203.0.113.77")).status_code == 200

    unlocked = await _events(db, EVENT_ACCOUNT_UNLOCKED)
    assert [(r.user_id, r.claimed_email, r.detail) for r in unlocked] == [
        ("u-cashier", CASHIER, "unlocked by admin u-owner"),
    ]
    assert (await _chain(db))[0::2] == ("intact", True)   # the unlock row is chained like the rest


@pytest.mark.asyncio
async def test_unlock_resets_the_count_but_is_not_an_exemption(device):
    admin, till = await _signed_in(device, OWNER), device()
    await _fail(till, CASHIER, 10)
    assert (await admin.post("/api/staff/u-cashier/unlock")).status_code == 204

    # A fresh budget: nine more wrong passwords are still short of a lock …
    await _fail(till, CASHIER, 9, subnet="192.0.2")
    assert (await _login(till, CASHIER, PASSWORD, ip="203.0.113.77")).status_code == 200

    # … and the account can be locked again like any other.
    await _fail(till, CASHIER, 10, subnet="100.64.0")
    assert (await _login(till, CASHIER, PASSWORD, ip="203.0.113.77")).status_code == 429


@pytest.mark.asyncio
async def test_a_non_admin_cannot_unlock(device, db):
    cashier = await _signed_in(device, CASHIER)
    accountant = await _signed_in(device, ACCOUNTANT)
    till = device()
    await _fail(till, OWNER, 10)

    assert (await cashier.post("/api/staff/u-owner/unlock")).status_code == 403
    assert (await accountant.post("/api/staff/u-owner/unlock")).status_code == 403
    assert (await device().post("/api/staff/u-owner/unlock")).status_code == 401

    assert await _events(db, EVENT_ACCOUNT_UNLOCKED) == []
    assert (await _login(till, OWNER, PASSWORD, ip="203.0.113.77")).status_code == 429


@pytest.mark.asyncio
async def test_unlocking_an_account_that_is_not_locked_is_harmless(device, db):
    admin, till = await _signed_in(device, OWNER), device()

    # Nothing to release: still a 204, and the account works as before.
    assert (await admin.post("/api/staff/u-cashier/unlock")).status_code == 204
    assert (await _login(till, CASHIER, PASSWORD, ip="203.0.113.77")).status_code == 200

    # It does not switch the lockout off either.
    await _fail(till, CASHIER, 10)
    assert (await _login(till, CASHIER, PASSWORD, ip="203.0.113.77")).status_code == 429
    assert len(await _events(db, EVENT_ACCOUNT_LOCKED, CASHIER)) == 1


@pytest.mark.asyncio
async def test_unlock_before_the_threshold_clears_the_failures_so_far(device):
    """Nine wrong passwords, a phone call to the owner, an unlock: the cashier
    must not be one typo away from a lock."""
    admin, till = await _signed_in(device, OWNER), device()
    await _fail(till, CASHIER, 9)
    assert (await admin.post("/api/staff/u-cashier/unlock")).status_code == 204

    await _fail(till, CASHIER, 9, subnet="192.0.2")
    assert (await _login(till, CASHIER, PASSWORD, ip="203.0.113.77")).status_code == 200


@pytest.mark.asyncio
async def test_unlock_releases_only_that_account(device):
    admin, till = await _signed_in(device, OWNER), device()
    await _fail(till, CASHIER, 10)
    await _fail(till, ACCOUNTANT, 10, subnet="192.0.2")

    assert (await admin.post("/api/staff/u-cashier/unlock")).status_code == 204

    assert (await _login(till, CASHIER, PASSWORD, ip="203.0.113.77")).status_code == 200
    assert (await _login(till, ACCOUNTANT, PASSWORD, ip="203.0.113.78")).status_code == 429


@pytest.mark.asyncio
async def test_a_signed_in_admin_can_release_their_own_account(device):
    """The owner's email is the obvious target. A lock only refuses NEW
    logins, so an owner who is already signed in can still lift it."""
    admin, phone = await _signed_in(device, OWNER), device()
    await _fail(phone, OWNER, 10)
    assert (await _login(phone, OWNER, PASSWORD, ip="203.0.113.77")).status_code == 429

    assert (await admin.post("/api/staff/u-owner/unlock")).status_code == 204
    assert (await _login(phone, OWNER, PASSWORD, ip="203.0.113.77")).status_code == 200


@pytest.mark.asyncio
async def test_unlock_of_an_unknown_user_is_404(device, db):
    admin = await _signed_in(device, OWNER)
    assert (await admin.post("/api/staff/no-such-user/unlock")).status_code == 404
    assert await _events(db, EVENT_ACCOUNT_UNLOCKED) == []


@pytest.mark.asyncio
async def test_an_unlock_older_than_the_lock_does_not_release_it(device, db):
    """Only an unlock issued AFTER the lock counts — yesterday's must not
    make an account unlockable for good."""
    till = device()
    await _seed_event(db, EVENT_ACCOUNT_UNLOCKED, CASHIER, minutes_ago=5)
    await _fail(till, CASHIER, 10)
    assert (await _login(till, CASHIER, PASSWORD, ip="203.0.113.77")).status_code == 429
