from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.auth_audit import (
    EVENT_LOGIN_FAILED,
    EVENT_LOGIN_SUCCESS,
    EVENT_LOGOUT,
    EVENT_PASSWORD_CHANGED,
    fire_auth_event,
    get_client_ip,
)
from app.core.rate_limit import limiter
from app.core.security import create_access_token, hash_password, verify_password
from app.deps import AUTH_COOKIE_NAME, get_current_user, get_db
from app.models import User
from app.schemas.auth import ChangePasswordRequest, LoginRequest, TokenResponse, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        max_age=settings.jwt_expires_minutes * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path="/",
    )


def _ua(request: Request) -> str | None:
    return request.headers.get("user-agent")


@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/minute")
async def login(
    request: Request,
    response: Response,
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """Authenticate a user and set the session cookie.

    AUDIT (phase A3b): emits LOGIN_SUCCESS or LOGIN_FAILED via
    `fire_auth_event`, which schedules the write on the event loop without
    awaiting it. This works for BOTH the success path (the function
    returns normally) and the failure path (raises HTTPException). FastAPI's
    `BackgroundTasks` only fire on successful return, which would silently
    drop failed-login events — the most important ones to audit.

    Note: requests rejected by the rate limiter (HTTP 429) short-circuit
    before this function body runs and are not currently audited. Future
    work: custom slowapi handler that emits LOGIN_RATE_LIMITED events.
    """
    client_ip = get_client_ip(request)
    ua = _ua(request)

    user = (
        await db.execute(select(User).where(User.email == body.email))
    ).scalar_one_or_none()

    if not user or not verify_password(body.password, user.password_hash):
        fire_auth_event(
            event_type=EVENT_LOGIN_FAILED,
            user_id=None,                # email is claimed-but-unverified
            claimed_email=body.email,
            client_ip=client_ip,
            user_agent=ua,
            detail="invalid credentials",
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if not user.is_active:
        fire_auth_event(
            event_type=EVENT_LOGIN_FAILED,
            user_id=user.id,             # we know who tried — they're disabled
            claimed_email=body.email,
            client_ip=client_ip,
            user_agent=ua,
            detail="account disabled",
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account disabled")

    token = create_access_token(subject=user.id, extra={"role": user.role.value})
    _set_auth_cookie(response, token)

    fire_auth_event(
        event_type=EVENT_LOGIN_SUCCESS,
        user_id=user.id,
        claimed_email=body.email,
        client_ip=client_ip,
        user_agent=ua,
    )

    return TokenResponse(access_token=token, user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return UserOut.model_validate(user)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response):
    """Clear the auth cookie. Best-effort audit even if the caller had no
    valid session (they may have been holding a stale cookie)."""
    response.delete_cookie(key=AUTH_COOKIE_NAME, path="/")
    fire_auth_event(
        event_type=EVENT_LOGOUT,
        user_id=None,           # request session may already be expired; can't trust it
        claimed_email=None,
        client_ip=get_client_ip(request),
        user_agent=_ua(request),
    )
    return None


@router.post("/change-password", status_code=204)
async def change_password(
    request: Request,
    body: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
    user.password_hash = hash_password(body.new_password)
    await db.commit()

    fire_auth_event(
        event_type=EVENT_PASSWORD_CHANGED,
        user_id=user.id,
        claimed_email=user.email,
        client_ip=get_client_ip(request),
        user_agent=_ua(request),
    )
