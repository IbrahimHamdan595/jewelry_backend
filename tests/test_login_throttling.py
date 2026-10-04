"""Login throttling (NEX-47): the per-IP rate limit and the per-account lockout.

Both layers are exercised through the real `/api/auth/login` route on the real
app, because the bugs they fix lived in the wiring: the limiter was keyed on
the load balancer's address, and nothing limited attempts per account at all.

`ip=` on the helpers sets X-Forwarded-For the way Render's ingress does
("<client>, <proxy hop>"); the socket peer is the same for every request, as it
is in production.
"""
import asyncio

import bcrypt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from app.core.rate_limit import client_ip_key, limiter
from app.models import Role, User

PASSWORD = "correct-horse-battery"
OWNER = "owner@example.com"
CASHIER = "cashier@example.com"


def _fast_hash(password: str) -> str:
    """bcrypt at the minimum cost: the tests log in dozens of times."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()


async def _settle() -> None:
    """Let the fire-and-forget audit writes finish before the next request."""
    pending = [
        t for t in asyncio.all_tasks()
        if t is not asyncio.current_task()
        and getattr(t.get_coro(), "__name__", "") == "record_auth_event_safe"
    ]
    await asyncio.gather(*pending)


@pytest_asyncio.fixture
async def client(db, monkeypatch):
    from app.deps import get_db
    from app.main import app

    hashed = _fast_hash(PASSWORD)
    db.add(User(id="u-owner", email=OWNER, name="Owner", password_hash=hashed, role=Role.ADMIN, is_active=True))
    db.add(User(id="u-cashier", email=CASHIER, name="Cashier", password_hash=hashed, role=Role.CASHIER, is_active=True))
    await db.commit()

    async def _get_db():
        yield db

    # The background recorder opens its own session on the app's engine; point
    # it at this test's database so audit rows land where the test can see them.
    monkeypatch.setattr(
        "app.core.auth_audit.async_session_factory",
        async_sessionmaker(db.bind, expire_on_commit=False, class_=AsyncSession),
    )
    limiter.reset()  # the limiter's memory storage outlives a single test
    app.dependency_overrides[get_db] = _get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    await _settle()


async def _login(client, email: str, password: str, *, ip: str):
    resp = await client.post(
        "/api/auth/login",
        json={"email": email, "password": password},
        headers={"X-Forwarded-For": f"{ip}, 10.0.0.1"},
    )
    await _settle()
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
