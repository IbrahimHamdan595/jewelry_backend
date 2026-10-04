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
from app.core.login_lockout import LOCKOUT_DETAIL, is_locked, record_failed_login
from app.core.rate_limit import limiter
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    create_access_token,
    hash_password,
    revoke_sessions,
    verify_password,
)
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


def _issue_token(user: User) -> str:
    # `role` is what the frontend middleware reads; `ver` is what
    # get_current_user compares against users.token_version (NEX-54).
    return create_access_token(
        subject=user.id,
        extra={"role": user.role.value, "ver": user.token_version},
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

    The one write that IS awaited is the wrong-password LOGIN_FAILED row:
    the per-account lockout is counted from it (see
    `app/core/login_lockout.py` for why it cannot be fire-and-forget).

    THROTTLING (NEX-47), two layers:
      • 5/minute per client IP (the decorator above).
      • Per account: 10 consecutive failures inside 15 minutes lock the
        claimed email for 15 minutes. The lock is checked BEFORE the user
        lookup and the password check and is keyed on the claimed email
        alone, so the response is identical whether or not the account
        exists, and a correct password is refused too while it holds.
        Same 429 as the rate limiter: both mean "too many attempts".

    Note: requests rejected with HTTP 429 — by the rate limiter, which
    short-circuits before this function body runs, or by the lockout — are
    not audited individually. The lockout itself is (ACCOUNT_LOCKED).
    Future work: custom slowapi handler that emits LOGIN_RATE_LIMITED events.
    """
    client_ip = get_client_ip(request)
    ua = _ua(request)

    if await is_locked(db, body.email):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=LOCKOUT_DETAIL)

    user = (
        await db.execute(select(User).where(User.email == body.email))
    ).scalar_one_or_none()

    # NEX-54: verify on EVERY attempt, account or no account. This used to be
    # `if not user or not verify_password(...)`, which returned before bcrypt
    # ran for an unknown email: ~1 ms against a few hundred for a real one —
    # a stopwatch was enough to tell which emails have accounts. The dummy is
    # a real hash of the same cost, so both paths do one bcrypt verification.
    password_ok = verify_password(
        body.password, user.password_hash if user else DUMMY_PASSWORD_HASH
    )
    if not user or not password_ok:
        await record_failed_login(db, claimed_email=body.email, client_ip=client_ip, user_agent=ua)
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

    token = _issue_token(user)
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
    valid session (they may have been holding a stale cookie).

    Deliberately does NOT bump token_version (NEX-54). Shop terminals may
    share one account, and logging one till out must not log out the till
    next to it. This ends THIS device's session by dropping its cookie;
    ending all of a user's sessions is a password change or an admin
    force-logout (`POST /staff/{id}/force-logout`).
    """
    response.delete_cookie(key=AUTH_COOKIE_NAME, path="/")
    fire_auth_event(
        event_type=EVENT_LOGOUT,
        user_id=None,           # request session may already be expired; can't trust it
        claimed_email=None,
        client_ip=get_client_ip(request),
        user_agent=_ua(request),
    )
    return None


@router.post("/change-password", response_model=TokenResponse)
async def change_password(
    request: Request,
    response: Response,
    body: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change the caller's password and end every other session of theirs.

    NEX-54: replacing the hash used to invalidate nothing — a token minted
    before the change stayed valid for the rest of its 8 hours, which is
    exactly the window someone who knew the old password needs. The same
    transaction now bumps token_version, so every token issued so far is
    refused from the next request on.

    That includes the one this request arrived with, so a fresh token is
    issued in the same response (cookie + body, the login shape) and the
    person who changed their own password stays signed in on this device.
    """
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
    user.password_hash = hash_password(body.new_password)
    await revoke_sessions(db, user)
    await db.commit()

    token = _issue_token(user)
    _set_auth_cookie(response, token)

    fire_auth_event(
        event_type=EVENT_PASSWORD_CHANGED,
        user_id=user.id,
        claimed_email=user.email,
        client_ip=get_client_ip(request),
        user_agent=_ua(request),
    )

    return TokenResponse(access_token=token, user=UserOut.model_validate(user))
