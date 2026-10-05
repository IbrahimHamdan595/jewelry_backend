from typing import AsyncGenerator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.db.session import async_session_factory
from app.models import User

bearer = HTTPBearer(auto_error=False)

AUTH_COOKIE_NAME = "mz_token"


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    token = credentials.credentials if credentials else request.cookies.get(AUTH_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload = decode_token(token)
        user_id: str = payload.get("sub", "")
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    # NEX-54: a token is only good for the token_version it was issued under.
    # A password change or an admin force-logout bumps the column, which ends
    # every outstanding session on its very next request — the same way
    # is_active takes effect immediately, because the user is re-read here.
    # A token WITHOUT the claim was minted before the claim existed and counts
    # as version 0, every user's starting value, so the sessions that are
    # alive when this deploys keep working until their first bump.
    if payload.get("ver", 0) != user.token_version:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    return user
