"""A new password longer than bcrypt can read is refused, cleanly (NEX-54).

bcrypt reads the first 72 BYTES of a password and nothing after them. bcrypt 5
raises on anything longer, so setting an over-long password was an unhandled
ValueError — a 500 — on every route that sets one. It is now a 422 that says
what is wrong.

Refused rather than silently truncated: a password manager's 80-character
password that is quietly cut to 72 is a password whose last eight characters
protect nothing, and its owner would never know.

(Verifying is different: `verify_password` still truncates, because hashes made
by older bcrypt releases were built from the truncated bytes. That is pinned
in tests/test_login_timing.py.)
"""
import pytest
from sqlalchemy import select

from app.core.security import MAX_PASSWORD_BYTES, PasswordTooLongError, hash_password, verify_password
from app.models import User
from tests.conftest import AUTH_PASSWORD as PASSWORD
from tests.conftest import CASHIER_EMAIL as CASHIER
from tests.conftest import OWNER_EMAIL as OWNER

FITS = "p" * 72                 # exactly the limit
TOO_LONG = "p" * 73             # one byte over
ACCENTED_FITS = "é" * 36        # 36 characters, 72 bytes
ACCENTED_TOO_LONG = "é" * 37    # 37 characters — short by any character count — but 74 bytes


async def _signed_in(device, email: str):
    client = device()
    resp = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return client


async def _can_log_in(device, email: str, password: str) -> bool:
    resp = await device().post("/api/auth/login", json={"email": email, "password": password})
    return resp.status_code == 200


def _assert_clean_422(resp) -> None:
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    # A sentence the UI can show as it is, naming the limit.
    assert isinstance(detail, str)
    assert "too long" in detail and "72 bytes" in detail


# ── The rule itself ───────────────────────────────────────────────────────────

def test_limit_is_what_bcrypt_reads():
    assert MAX_PASSWORD_BYTES == 72


@pytest.mark.parametrize("password", [FITS, ACCENTED_FITS], ids=["ascii-72", "accented-72"])
def test_a_password_at_the_limit_is_hashed(password):
    assert verify_password(password, hash_password(password)) is True


@pytest.mark.parametrize("password", [TOO_LONG, ACCENTED_TOO_LONG, "p" * 500], ids=["ascii-73", "accented-74", "ascii-500"])
def test_a_password_over_the_limit_is_refused_by_name(password):
    """Every route that sets a password goes through hash_password, and so
    does the seed script — which gets this message instead of bcrypt's."""
    with pytest.raises(PasswordTooLongError) as excinfo:
        hash_password(password)
    message = str(excinfo.value)
    assert "72 bytes" in message and str(len(password.encode())) in message
    assert password not in message                      # never echo the password
    assert isinstance(excinfo.value, ValueError)


# ── Change password ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("new_password", [TOO_LONG, ACCENTED_TOO_LONG], ids=["ascii-73", "accented-74"])
async def test_change_password_refuses_an_over_long_password(device, db, new_password):
    till = await _signed_in(device, CASHIER)

    resp = await till.post(
        "/api/auth/change-password",
        json={"current_password": PASSWORD, "new_password": new_password},
    )
    _assert_clean_422(resp)

    # Nothing happened: same password, same sessions.
    assert (await db.execute(select(User.token_version).where(User.email == CASHIER))).scalar_one() == 0
    assert (await till.get("/api/auth/me")).status_code == 200
    assert await _can_log_in(device, CASHIER, PASSWORD)


@pytest.mark.asyncio
@pytest.mark.parametrize("new_password", [FITS, ACCENTED_FITS], ids=["ascii-72", "accented-72"])
async def test_change_password_accepts_a_password_at_the_limit(device, new_password):
    till = await _signed_in(device, CASHIER)

    resp = await till.post(
        "/api/auth/change-password",
        json={"current_password": PASSWORD, "new_password": new_password},
    )
    assert resp.status_code == 200, resp.text
    assert await _can_log_in(device, CASHIER, new_password)


# ── Staff ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_staff_create_refuses_an_over_long_password(device, db):
    admin = await _signed_in(device, OWNER)

    resp = await admin.post("/api/staff", json={"email": "new@example.com", "name": "New", "password": TOO_LONG})
    _assert_clean_422(resp)
    assert (await db.execute(select(User).where(User.email == "new@example.com"))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_staff_create_accepts_a_password_at_the_limit(device):
    admin = await _signed_in(device, OWNER)

    resp = await admin.post("/api/staff", json={"email": "new@example.com", "name": "New", "password": FITS})
    assert resp.status_code == 201, resp.text
    assert await _can_log_in(device, "new@example.com", FITS)


@pytest.mark.asyncio
async def test_staff_update_refuses_an_over_long_password_and_changes_nothing(device, db):
    """The rename in the same request must not slip through on its own."""
    admin = await _signed_in(device, OWNER)
    till = await _signed_in(device, CASHIER)

    resp = await admin.patch("/api/staff/u-cashier", json={"name": "Renamed", "password": TOO_LONG})
    _assert_clean_422(resp)

    name, version = (await db.execute(
        select(User.name, User.token_version).where(User.id == "u-cashier")
    )).one()
    assert (name, version) == ("Cashier", 0)
    assert (await till.get("/api/auth/me")).status_code == 200
    assert await _can_log_in(device, CASHIER, PASSWORD)


@pytest.mark.asyncio
async def test_staff_update_accepts_a_password_at_the_limit(device):
    admin = await _signed_in(device, OWNER)

    resp = await admin.patch("/api/staff/u-cashier", json={"password": ACCENTED_FITS})
    assert resp.status_code == 200, resp.text
    assert await _can_log_in(device, CASHIER, ACCENTED_FITS)
