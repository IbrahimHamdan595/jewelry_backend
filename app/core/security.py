from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import User


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_access_token(subject: str, extra: dict[str, Any] | None = None) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expires_minutes)
    payload: dict[str, Any] = {"sub": subject, "exp": expire}
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


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
