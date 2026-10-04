from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import bcrypt
from jose import JWTError, jwk, jwt
from jose.backends.base import Key
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import User

# ── JWT signing (NEX-54) ──────────────────────────────────────────────────────
# Two ways to sign, and the SERVER'S configuration picks between them — never
# the token:
#
#   RS256        JWT_PRIVATE_KEY signs, the public key verifies. The frontend
#                middleware gets the public key only, so it can check a session
#                but can never mint one.
#   shared secret (HS256 unless JWT_ALGORITHM names another HMAC) JWT_SECRET
#                both signs and verifies — which is why the frontend holding it
#                was a problem. Still what is used while no key is configured,
#                and still ACCEPTED while JWT_ACCEPT_HS256 is true so sessions
#                issued before the switch survive it.
_RSA_ALGORITHM = "RS256"
_HMAC_ALGORITHMS = ("HS256", "HS384", "HS512")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def _shared_secret_algorithm() -> str:
    """Algorithm of the shared-secret path: JWT_ALGORITHM, as it always was.

    JWT_ALGORITHM never selects RS256 — JWT_PRIVATE_KEY does. The frontend
    needs JWT_ALGORITHM=RS256 on its side, so the same value may well get
    mirrored onto the backend; anything that is not an HMAC algorithm is
    read as HS256 rather than being handed JWT_SECRET as an RSA key.
    """
    return settings.jwt_algorithm if settings.jwt_algorithm in _HMAC_ALGORITHMS else "HS256"


@lru_cache(maxsize=4)
def _load_rsa_keys(private_pem: str, public_pem: str) -> tuple[Key | None, Key | None]:
    """(signing key, verifying key) for the configured PEMs; None where unset.

    Parsed once per distinct configuration: loading an RSA private key costs
    tens of milliseconds, far too much to repeat on every request. A missing
    JWT_PUBLIC_KEY is derived from the private key, so the backend can never
    issue tokens it would then refuse itself.
    """
    private = jwk.construct(private_pem, _RSA_ALGORITHM) if private_pem else None
    if public_pem:
        public = jwk.construct(public_pem, _RSA_ALGORITHM)
    else:
        public = private.public_key() if private else None
    return private, public


def _rsa_keys() -> tuple[Key | None, Key | None]:
    return _load_rsa_keys(settings.jwt_private_key_pem, settings.jwt_public_key_pem)


def check_jwt_keys() -> None:
    """Refuse to start with RSA keys that cannot work.

    Called at import, so a bad key fails the deploy (the previous release
    keeps serving) instead of surfacing as "nobody can log in": an unparsable
    PEM, a public key pasted where the private one belongs, or a public key
    that does not belong to the private key — which would make the backend
    reject every token it issues. A no-op while no key is configured.

    The messages name the variable and never include key material.
    """
    try:
        private, public = _rsa_keys()
    except Exception:
        raise RuntimeError(
            "JWT_PRIVATE_KEY / JWT_PUBLIC_KEY is set but is not a readable RSA key in PEM format"
        ) from None
    if private is None:
        return
    try:
        probe = jwt.encode({"sub": "startup-self-check"}, private, algorithm=_RSA_ALGORITHM)
    except Exception:
        raise RuntimeError("JWT_PRIVATE_KEY cannot sign: it is not an RSA private key") from None
    try:
        jwt.decode(probe, public, algorithms=[_RSA_ALGORITHM])
    except Exception:
        raise RuntimeError("JWT_PUBLIC_KEY does not match JWT_PRIVATE_KEY") from None


check_jwt_keys()


def create_access_token(subject: str, extra: dict[str, Any] | None = None) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expires_minutes)
    payload: dict[str, Any] = {"sub": subject, "exp": expire}
    if extra:
        payload.update(extra)
    private, _ = _rsa_keys()
    if private is not None:
        return jwt.encode(payload, private, algorithm=_RSA_ALGORITHM)
    return jwt.encode(payload, settings.jwt_secret, algorithm=_shared_secret_algorithm())


def decode_token(token: str) -> dict[str, Any]:
    """Verify a token with one of the verifiers the SERVER allows. Raises JWTError.

    The token's own (unverified) `alg` header is used for exactly one thing:
    choosing between the at most two verifiers configured here. Each verifier
    is pinned to its own key AND its own algorithm, so the header can never
    talk us into a pairing we did not choose:

      • RS256 → the RSA public key, RS256 only. Only if a key is configured.
      • HMAC  → JWT_SECRET, the configured HMAC algorithm only. Only while
                JWT_ACCEPT_HS256 is true (and the secret is not empty).

    That closes the classic algorithm-confusion forgery — an HS256 token
    signed with the PUBLIC key's bytes as the HMAC secret: an HMAC token is
    only ever checked against JWT_SECRET, never against the RSA key. And
    `alg: none`, like any algorithm not listed above, matches no verifier
    and is refused before any key is touched.
    """
    alg = jwt.get_unverified_header(token).get("alg")
    _, public = _rsa_keys()
    if alg == _RSA_ALGORITHM and public is not None:
        return jwt.decode(token, public, algorithms=[_RSA_ALGORITHM])
    shared = _shared_secret_algorithm()
    if alg == shared and settings.jwt_accept_hs256 and settings.jwt_secret:
        return jwt.decode(token, settings.jwt_secret, algorithms=[shared])
    raise JWTError("Token algorithm is not accepted")


async def revoke_sessions(db: AsyncSession, user: User) -> None:
    """End every session `user` has, on every device. Does NOT commit.

    Every token carries the `token_version` it was issued under and
    `get_current_user` refuses any other, so one increment invalidates them
    all on their next request.

    The increment is done in SQL, not in Python, so two revocations racing
    each other both land. The refresh then reads the new value back inside
    the same transaction — our own UPDATE holds the row lock until commit —
    and that is the version a token reissued by the caller has to carry.
    """
    user.token_version = User.token_version + 1
    await db.flush()
    await db.refresh(user, attribute_names=["token_version"])
