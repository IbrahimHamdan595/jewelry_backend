"""JWT signing keys: HS256 today, RS256 once keys are provisioned (NEX-54).

The Next.js middleware verifies the session token itself. With HS256 that
means Vercel holds the very secret that MINTS tokens. With RS256 the backend
keeps the private key and the frontend gets a public key that can only verify.

What has to hold, and is pinned here:

  • no new env vars → byte-for-byte today's behaviour (HS256 with JWT_SECRET)
  • JWT_PRIVATE_KEY set → new tokens are RS256, same claims
  • during the migration window both kinds verify; JWT_ACCEPT_HS256=false
    ends the window
  • the token's own `alg` header only ever selects between the verifiers the
    SERVER allows, each pinned to its own key: no algorithm confusion, no
    `alg: none`

The attack tokens are built by hand (base64 + hmac) on purpose: python-jose
refuses to produce them, and a forger would not be using python-jose.
"""
import base64
import hashlib
import hmac
import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient
from jose import JWTError, jwt

from app.config import Settings, settings
from app.core import security
from app.core.security import create_access_token, decode_token
from app.deps import AUTH_COOKIE_NAME
from tests.conftest import AUTH_PASSWORD as PASSWORD
from tests.conftest import OWNER_EMAIL as OWNER


def _generate_keypair() -> tuple[str, str]:
    """What the README's openssl commands produce: PKCS#8 private, SPKI public."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return private, public


@pytest.fixture(scope="module")
def keypair():
    return _generate_keypair()


@pytest.fixture(scope="module")
def stranger_keypair():
    """Someone else's perfectly valid RSA key."""
    return _generate_keypair()


@pytest.fixture
def rs256(monkeypatch, keypair):
    """The backend after keys are provisioned (JWT_ACCEPT_HS256 still at its default)."""
    private, public = keypair
    monkeypatch.setattr(settings, "jwt_private_key", private)
    monkeypatch.setattr(settings, "jwt_public_key", public)
    return keypair


def _alg(token: str) -> str:
    return jwt.get_unverified_header(token)["alg"]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _forge(header: dict, hmac_secret: bytes | None = None, **claims) -> str:
    """A token assembled by hand. `hmac_secret=None` leaves the signature empty."""
    payload = {"sub": "u-owner", "role": "ADMIN", "ver": 0, "exp": int(time.time()) + 3600, **claims}
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(payload).encode())}"
    signature = (
        _b64(hmac.new(hmac_secret, signing_input.encode(), hashlib.sha256).digest())
        if hmac_secret is not None else ""
    )
    return f"{signing_input}.{signature}"


def _rejected(token: str) -> bool:
    try:
        decode_token(token)
    except JWTError:
        return True
    return False


# ── Nothing provisioned: today's behaviour ────────────────────────────────────

def test_new_settings_default_to_todays_behaviour():
    fields = Settings.model_fields
    assert fields["jwt_private_key"].default == ""
    assert fields["jwt_public_key"].default == ""
    assert fields["jwt_accept_hs256"].default is True


def test_without_keys_tokens_are_hs256_signed_with_the_secret():
    token = create_access_token(subject="u-owner", extra={"role": "ADMIN", "ver": 0})
    assert _alg(token) == "HS256"
    # Verifiable by anything holding JWT_SECRET, exactly as the frontend does today.
    claims = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    assert (claims["sub"], claims["role"]) == ("u-owner", "ADMIN")
    assert decode_token(token) == claims


def test_without_keys_an_rs256_token_is_refused(keypair):
    private, _ = keypair
    token = jwt.encode({"sub": "u-owner", "role": "ADMIN"}, private, algorithm="RS256")
    assert _rejected(token)


def test_the_existing_jwt_algorithm_setting_is_still_honoured(monkeypatch):
    """A deployment already running HS384/HS512 keeps doing so."""
    monkeypatch.setattr(settings, "jwt_algorithm", "HS512")
    token = create_access_token(subject="u-owner")
    assert _alg(token) == "HS512"
    assert decode_token(token)["sub"] == "u-owner"


# ── Keys provisioned ──────────────────────────────────────────────────────────

def test_with_a_private_key_new_tokens_are_rs256(rs256):
    token = create_access_token(subject="u-owner", extra={"role": "ADMIN", "ver": 3})
    assert _alg(token) == "RS256"
    assert decode_token(token)["sub"] == "u-owner"


def test_claims_do_not_change_with_the_algorithm(monkeypatch, keypair):
    """The frontend middleware reads `role`; nothing about the payload moves."""
    before = jwt.get_unverified_claims(create_access_token(subject="u-owner", extra={"role": "ADMIN", "ver": 0}))
    monkeypatch.setattr(settings, "jwt_private_key", keypair[0])
    after = jwt.get_unverified_claims(create_access_token(subject="u-owner", extra={"role": "ADMIN", "ver": 0}))
    assert set(after) == set(before) == {"sub", "exp", "role", "ver"}
    assert (after["sub"], after["role"], after["ver"]) == (before["sub"], before["role"], before["ver"])


def test_the_public_key_alone_verifies_but_cannot_sign(rs256):
    """The frontend's position: it can check a token, it can never mint one."""
    _, public = rs256
    token = create_access_token(subject="u-owner", extra={"role": "ADMIN", "ver": 0})

    assert jwt.decode(token, public, algorithms=["RS256"])["role"] == "ADMIN"
    with pytest.raises(Exception):
        jwt.encode({"sub": "u-owner", "role": "ADMIN"}, public, algorithm="RS256")


def test_keys_with_literal_backslash_n_are_accepted(monkeypatch, keypair):
    """Env-var UIs often store a PEM on one line with the two characters `\\n`."""
    private, public = keypair
    monkeypatch.setattr(settings, "jwt_private_key", private.replace("\n", "\\n"))
    monkeypatch.setattr(settings, "jwt_public_key", public.replace("\n", "\\n"))
    assert "\n" not in settings.jwt_private_key

    token = create_access_token(subject="u-owner")
    assert _alg(token) == "RS256"
    assert jwt.decode(token, public, algorithms=["RS256"])["sub"] == "u-owner"
    assert decode_token(token)["sub"] == "u-owner"


def test_public_key_is_derived_when_only_the_private_key_is_set(monkeypatch, keypair):
    """Forgetting JWT_PUBLIC_KEY on the backend must not produce tokens the
    backend itself then refuses."""
    private, public = keypair
    monkeypatch.setattr(settings, "jwt_private_key", private)
    token = create_access_token(subject="u-owner")
    assert decode_token(token)["sub"] == "u-owner"
    assert jwt.decode(token, public, algorithms=["RS256"])["sub"] == "u-owner"


def test_backend_jwt_algorithm_set_to_rs256_does_not_break_the_legacy_path(monkeypatch, keypair):
    """Vercel needs JWT_ALGORITHM=RS256; an operator may mirror it onto Render.
    RS256 is chosen by JWT_PRIVATE_KEY alone, and the shared-secret path can
    only ever be an HMAC algorithm."""
    legacy = create_access_token(subject="u-owner")
    monkeypatch.setattr(settings, "jwt_algorithm", "RS256")
    assert _alg(create_access_token(subject="u-owner")) == "HS256"   # no keys yet
    assert decode_token(legacy)["sub"] == "u-owner"

    monkeypatch.setattr(settings, "jwt_private_key", keypair[0])
    assert _alg(create_access_token(subject="u-owner")) == "RS256"
    assert decode_token(legacy)["sub"] == "u-owner"                  # window still open


# ── Migration window ──────────────────────────────────────────────────────────

def test_hs256_sessions_survive_the_switch_while_the_window_is_open(monkeypatch, keypair):
    old_session = create_access_token(subject="u-owner", extra={"role": "ADMIN"})
    assert _alg(old_session) == "HS256"

    monkeypatch.setattr(settings, "jwt_private_key", keypair[0])
    monkeypatch.setattr(settings, "jwt_public_key", keypair[1])

    assert decode_token(old_session)["sub"] == "u-owner"
    assert _alg(create_access_token(subject="u-owner")) == "RS256"   # but nothing new is HS256


def test_closing_the_window_refuses_hs256(monkeypatch, rs256):
    old_session = jwt.encode({"sub": "u-owner", "role": "ADMIN"}, settings.jwt_secret, algorithm="HS256")
    assert decode_token(old_session)["sub"] == "u-owner"

    monkeypatch.setattr(settings, "jwt_accept_hs256", False)

    assert _rejected(old_session)
    assert decode_token(create_access_token(subject="u-owner"))["sub"] == "u-owner"   # RS256 unaffected


# ── Forgeries ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("accept_hs256", [True, False])
def test_algorithm_confusion_is_refused(monkeypatch, rs256, accept_hs256):
    """The classic: the public key is public, so sign an HS256 token with the
    public key's PEM as the HMAC secret and hope the server "verifies" it with
    the same bytes. It must never use the RSA key for an HMAC token."""
    _, public = rs256
    monkeypatch.setattr(settings, "jwt_accept_hs256", accept_hs256)

    for secret in (public, public.strip(), public.replace("\n", "\\n")):
        forged = _forge({"alg": "HS256", "typ": "JWT"}, hmac_secret=secret.encode())
        assert _rejected(forged)


@pytest.mark.parametrize("alg", ["none", "None", "NONE", "nOnE"])
def test_alg_none_is_refused(monkeypatch, keypair, alg):
    unsigned = _forge({"alg": alg, "typ": "JWT"})
    assert _rejected(unsigned)                       # HS256-only server

    monkeypatch.setattr(settings, "jwt_private_key", keypair[0])
    monkeypatch.setattr(settings, "jwt_public_key", keypair[1])
    assert _rejected(unsigned)                       # migration window
    monkeypatch.setattr(settings, "jwt_accept_hs256", False)
    assert _rejected(unsigned)                       # RS256-only server


def test_a_token_signed_by_another_rsa_key_is_refused(rs256, stranger_keypair):
    forged = jwt.encode({"sub": "u-owner", "role": "ADMIN"}, stranger_keypair[0], algorithm="RS256")
    assert _rejected(forged)


def test_an_hs256_token_signed_with_the_wrong_secret_is_refused(rs256):
    assert _rejected(_forge({"alg": "HS256", "typ": "JWT"}, hmac_secret=b"not-the-secret"))


def test_an_empty_secret_never_verifies_anything(monkeypatch, rs256):
    """If JWT_SECRET is ever blanked instead of JWT_ACCEPT_HS256 being turned
    off, an HMAC keyed with "" must not become a skeleton key."""
    monkeypatch.setattr(settings, "jwt_secret", "")
    assert _rejected(_forge({"alg": "HS256", "typ": "JWT"}, hmac_secret=b""))


@pytest.mark.parametrize("token", ["", "garbage", "a.b", "a.b.c", "....", "e30.e30."])
def test_malformed_tokens_are_a_jwt_error_not_a_crash(rs256, token):
    assert _rejected(token)


# ── Startup self-check ────────────────────────────────────────────────────────

def test_startup_check_is_a_no_op_without_keys():
    security.check_jwt_keys()


def test_startup_check_passes_for_a_matching_pair(rs256):
    security.check_jwt_keys()


def test_startup_check_refuses_a_public_key_that_does_not_match(monkeypatch, keypair, stranger_keypair):
    """Otherwise every token the backend issued would be refused by the backend."""
    monkeypatch.setattr(settings, "jwt_private_key", keypair[0])
    monkeypatch.setattr(settings, "jwt_public_key", stranger_keypair[1])
    with pytest.raises(RuntimeError, match="does not match"):
        security.check_jwt_keys()


@pytest.mark.parametrize("field", ["jwt_private_key", "jwt_public_key"])
def test_startup_check_refuses_a_key_that_is_not_pem(monkeypatch, field):
    monkeypatch.setattr(settings, field, "-----BEGIN PRIVATE KEY-----\\nnot-a-key\\n-----END PRIVATE KEY-----")
    with pytest.raises(RuntimeError) as excinfo:
        security.check_jwt_keys()
    assert "not-a-key" not in str(excinfo.value)     # never echo key material


def test_startup_check_refuses_a_public_key_in_the_private_slot(monkeypatch, keypair):
    monkeypatch.setattr(settings, "jwt_private_key", keypair[1])
    with pytest.raises(RuntimeError, match="JWT_PRIVATE_KEY"):
        security.check_jwt_keys()


# ── End to end through the API ────────────────────────────────────────────────

@pytest.fixture
def client(device):
    return device()


async def _me(client: AsyncClient, token: str) -> int:
    return (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})).status_code


@pytest.mark.asyncio
async def test_rs256_login_works_end_to_end(client, rs256):
    _, public = rs256
    resp = await client.post("/api/auth/login", json={"email": OWNER, "password": PASSWORD})
    assert resp.status_code == 200, resp.text

    cookie = resp.cookies[AUTH_COOKIE_NAME]
    assert cookie == resp.json()["access_token"]
    assert _alg(cookie) == "RS256"
    # What the Next.js middleware does with it: public key in, role out.
    assert jwt.decode(cookie, public, algorithms=["RS256"])["role"] == "ADMIN"
    # And the backend accepts the session it just issued (cookie is in the jar).
    assert (await client.get("/api/auth/me")).status_code == 200


@pytest.mark.asyncio
async def test_forged_tokens_get_401_from_the_api(client, rs256, stranger_keypair):
    _, public = rs256
    forgeries = {
        "alg confusion": _forge({"alg": "HS256", "typ": "JWT"}, hmac_secret=public.encode()),
        "alg none": _forge({"alg": "none", "typ": "JWT"}),
        "wrong rsa key": jwt.encode({"sub": "u-owner", "role": "ADMIN"}, stranger_keypair[0], algorithm="RS256"),
        "wrong secret": _forge({"alg": "HS256", "typ": "JWT"}, hmac_secret=b"not-the-secret"),
    }
    assert {name: await _me(client, token) for name, token in forgeries.items()} == dict.fromkeys(forgeries, 401)

    # Control: the same claims, properly signed, are a session.
    genuine = create_access_token(subject="u-owner", extra={"role": "ADMIN", "ver": 0})
    assert await _me(client, genuine) == 200


@pytest.mark.asyncio
async def test_hs256_session_is_cut_off_when_the_window_closes(client, monkeypatch, keypair):
    resp = await client.post("/api/auth/login", json={"email": OWNER, "password": PASSWORD})
    old_session = resp.json()["access_token"]
    assert _alg(old_session) == "HS256"
    client.cookies.clear()

    monkeypatch.setattr(settings, "jwt_private_key", keypair[0])
    monkeypatch.setattr(settings, "jwt_public_key", keypair[1])
    assert await _me(client, old_session) == 200     # window open

    monkeypatch.setattr(settings, "jwt_accept_hs256", False)
    assert await _me(client, old_session) == 401     # window closed
