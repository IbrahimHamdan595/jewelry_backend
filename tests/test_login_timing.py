"""Login must not reveal which emails exist by how long it takes (NEX-54).

`/auth/login` used to be `if not user or not verify_password(...)`: for an
unknown email the `or` short-circuited and the request returned before bcrypt
ran — about a millisecond, against a few hundred for a real account. Anyone
could sort a list of addresses into "has an account" and "does not" with a
stopwatch.

No wall-clock assertions here (they flake). What is pinned instead is the
cause: BOTH paths perform exactly one bcrypt verification, against hashes of
the same cost.
"""
from pathlib import Path
from unittest.mock import patch

import bcrypt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import security
from app.core.rate_limit import limiter
from app.core.security import DUMMY_PASSWORD_HASH, hash_password, verify_password
from app.models import Role, User

PASSWORD = "correct-horse-battery"
OWNER = "owner@example.com"
GHOST = "nobody-here@example.com"


def _cost(hashed: str) -> int:
    """The work factor in a bcrypt hash: `$2b$12$...` → 12."""
    return int(hashed.split("$")[2])


@pytest_asyncio.fixture
async def client(db, monkeypatch):
    from app.deps import get_db
    from app.main import app

    owner_hash = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
    db.add(User(id="u-owner", email=OWNER, name="Owner", password_hash=owner_hash, role=Role.ADMIN, is_active=True))
    await db.commit()

    async def _get_db():
        yield db

    monkeypatch.setattr(
        "app.core.auth_audit.async_session_factory",
        async_sessionmaker(db.bind, expire_on_commit=False, class_=AsyncSession),
    )
    limiter.reset()
    app.dependency_overrides[get_db] = _get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.owner_hash = owner_hash
        yield c
    app.dependency_overrides.clear()


async def _attempt(client, email: str, password: str):
    """One login attempt, with every real bcrypt verification it caused."""
    with patch.object(security.bcrypt, "checkpw", wraps=bcrypt.checkpw) as checkpw:
        resp = await client.post("/api/auth/login", json={"email": email, "password": password})
    return resp, checkpw.call_args_list


# ── The dummy hash ────────────────────────────────────────────────────────────

def test_dummy_hash_is_a_real_bcrypt_hash():
    """It has to be verifiable — a malformed hash makes bcrypt raise at once,
    which would be the fast path all over again."""
    assert len(DUMMY_PASSWORD_HASH) == 60
    assert bcrypt.checkpw(b"anything at all", DUMMY_PASSWORD_HASH.encode()) is False


def test_dummy_hash_costs_what_a_real_hash_costs():
    """Same work factor as hash_password(), or the two paths differ again.
    If the cost is ever raised, this fails until the dummy is regenerated."""
    assert _cost(DUMMY_PASSWORD_HASH) == _cost(hash_password("whatever"))


def test_dummy_hash_is_precomputed_not_generated_per_process():
    """A literal in the source: no bcrypt at import, the same on every worker."""
    source = Path(security.__file__).read_text()
    assert f'DUMMY_PASSWORD_HASH = "{DUMMY_PASSWORD_HASH}"' in source


# ── One verification on every path ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_email_still_performs_one_bcrypt_verification(client):
    resp, calls = await _attempt(client, GHOST, "some-password")
    assert resp.status_code == 401
    assert len(calls) == 1
    assert calls[0].args == (b"some-password", DUMMY_PASSWORD_HASH.encode())


@pytest.mark.asyncio
async def test_wrong_password_performs_one_bcrypt_verification(client):
    resp, calls = await _attempt(client, OWNER, "some-password")
    assert resp.status_code == 401
    assert len(calls) == 1
    assert calls[0].args == (b"some-password", client.owner_hash.encode())


@pytest.mark.asyncio
async def test_successful_login_performs_one_bcrypt_verification(client):
    resp, calls = await _attempt(client, OWNER, PASSWORD)
    assert resp.status_code == 200
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unknown_email_and_wrong_password_answer_identically(client):
    unknown, _ = await _attempt(client, GHOST, "some-password")
    wrong, _ = await _attempt(client, OWNER, "some-password")
    assert (unknown.status_code, unknown.json()) == (wrong.status_code, wrong.json()) == (
        401, {"detail": "Invalid credentials"},
    )


# ── bcrypt's 72-byte limit ────────────────────────────────────────────────────
#
# bcrypt reads at most 72 bytes of a password. bcrypt 5 raises on anything
# longer instead of truncating, which made an over-long password a 500 — and,
# before the dummy verification, a 500 ONLY when the account existed.

LONG_PASSWORD = "x" * 200


@pytest.mark.asyncio
@pytest.mark.parametrize("email", [OWNER, GHOST])
async def test_over_long_password_is_an_ordinary_401_on_both_paths(client, email):
    resp, calls = await _attempt(client, email, LONG_PASSWORD)
    assert resp.status_code == 401
    assert len(calls) == 1
    assert calls[0].args[0] == b"x" * 72     # the same fixed-size input bcrypt would use


def test_verify_password_reads_the_first_72_bytes_like_bcrypt_does():
    hashed = bcrypt.hashpw(b"x" * 72, bcrypt.gensalt(rounds=4)).decode()
    assert verify_password("x" * 72, hashed) is True
    assert verify_password(LONG_PASSWORD, hashed) is True       # bytes 73+ never counted
    assert verify_password("x" * 71, hashed) is False


def test_verify_password_truncates_bytes_not_characters():
    """72 BYTES: a multi-byte character straddling the limit is cut mid-way,
    exactly as bcrypt itself would have cut it."""
    password = "é" * 50                                          # 100 bytes in UTF-8
    hashed = bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt(rounds=4)).decode()
    assert verify_password(password, hashed) is True
