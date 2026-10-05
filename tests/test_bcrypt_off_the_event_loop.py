"""bcrypt must not run on the event loop (NEX-54).

One bcrypt operation is a few hundred milliseconds of pure CPU. Called
straight from an `async def` route it holds the event loop for all of it, and
while the loop is held NOTHING else is served: not a sale, not /health. Login
used to be capped at five a minute for the whole internet, which hid this; a
per-IP limit, plus a verification for unknown emails too, means a burst of
logins is now a burst of bcrypt.

So every route that verifies or hashes a password hands the work to a worker
thread. Pinned two ways: each bcrypt call is observed on a thread that is not
the loop's, and a request made WHILE a verification is in progress is answered
before that verification finishes.
"""
import asyncio
import threading
from unittest.mock import patch

import bcrypt
import pytest

from app.core import security
from tests.conftest import AUTH_PASSWORD as PASSWORD
from tests.conftest import CASHIER_EMAIL as CASHIER
from tests.conftest import OWNER_EMAIL as OWNER

NEW_PASSWORD = "an-entirely-new-password"

# `security.bcrypt` IS the bcrypt module, so patching it patches bcrypt
# itself. The wrappers below must call the originals captured here, not
# `bcrypt.checkpw` — that would be the wrapper calling itself.
_checkpw, _hashpw, _gensalt = bcrypt.checkpw, bcrypt.hashpw, bcrypt.gensalt


class _Observed:
    """Runs the real bcrypt, remembering which thread each call ran on."""

    def __init__(self):
        self.threads: dict[str, list[int]] = {"checkpw": [], "hashpw": []}

    def checkpw(self, password, hashed):
        self.threads["checkpw"].append(threading.get_ident())
        return _checkpw(password, hashed)

    def hashpw(self, password, salt):
        self.threads["hashpw"].append(threading.get_ident())
        return _hashpw(password, salt)


@pytest.fixture
def observed():
    seen = _Observed()
    with patch.object(security.bcrypt, "checkpw", seen.checkpw), \
            patch.object(security.bcrypt, "hashpw", seen.hashpw), \
            patch.object(security.bcrypt, "gensalt", lambda: _gensalt(rounds=4)):
        yield seen


def _assert_off_loop(threads: list[int], *, calls: int) -> None:
    assert len(threads) == calls
    assert threading.get_ident() not in threads, "bcrypt ran on the event loop thread"


async def _signed_in(device, email: str):
    client = device()
    resp = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return client


# ── Which thread ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("email, password, expected", [
    (OWNER, PASSWORD, 200),
    (OWNER, "wrong-password", 401),
    ("nobody-here@example.com", "wrong-password", 401),     # the dummy-hash path
])
async def test_login_verifies_in_a_worker_thread(device, observed, email, password, expected):
    resp = await device().post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == expected
    _assert_off_loop(observed.threads["checkpw"], calls=1)


@pytest.mark.asyncio
async def test_change_password_verifies_and_hashes_in_a_worker_thread(device):
    till = await _signed_in(device, CASHIER)
    seen = _Observed()
    with patch.object(security.bcrypt, "checkpw", seen.checkpw), patch.object(security.bcrypt, "hashpw", seen.hashpw):
        resp = await till.post(
            "/api/auth/change-password",
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        )
    assert resp.status_code == 200, resp.text
    _assert_off_loop(seen.threads["checkpw"], calls=1)
    _assert_off_loop(seen.threads["hashpw"], calls=1)


@pytest.mark.asyncio
async def test_staff_create_hashes_in_a_worker_thread(device):
    admin = await _signed_in(device, OWNER)
    seen = _Observed()
    with patch.object(security.bcrypt, "hashpw", seen.hashpw):
        resp = await admin.post("/api/staff", json={"email": "new@example.com", "name": "New", "password": NEW_PASSWORD})
    assert resp.status_code == 201, resp.text
    _assert_off_loop(seen.threads["hashpw"], calls=1)


@pytest.mark.asyncio
async def test_staff_password_reset_hashes_in_a_worker_thread(device):
    admin = await _signed_in(device, OWNER)
    seen = _Observed()
    with patch.object(security.bcrypt, "hashpw", seen.hashpw):
        resp = await admin.patch("/api/staff/u-cashier", json={"password": NEW_PASSWORD})
    assert resp.status_code == 200, resp.text
    _assert_off_loop(seen.threads["hashpw"], calls=1)


# ── What it buys ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_other_requests_are_served_while_a_login_is_being_verified(device):
    """A bcrypt verification that will not finish until we say so. If it were
    running on the event loop, nothing below could be answered until it gave
    up; off the loop, /health comes back while the login is still waiting."""
    started, release = threading.Event(), threading.Event()

    def stuck_checkpw(password, hashed):
        started.set()
        release.wait(timeout=5)       # safety net: never hang the suite
        return _checkpw(password, hashed)

    client, bystander = device(), device()
    with patch.object(security.bcrypt, "checkpw", stuck_checkpw):
        login = asyncio.create_task(
            client.post("/api/auth/login", json={"email": OWNER, "password": PASSWORD})
        )
        try:
            # Let the login reach bcrypt …
            while not started.is_set():
                assert not login.done(), "login finished without verifying a password"
                await asyncio.sleep(0.01)

            # … and, with bcrypt still busy, do something else entirely.
            health = await asyncio.wait_for(bystander.get("/health"), timeout=2)
            assert health.status_code == 200
            assert not login.done(), "the login was not actually still in progress"
        finally:
            release.set()
        assert (await login).status_code == 200
