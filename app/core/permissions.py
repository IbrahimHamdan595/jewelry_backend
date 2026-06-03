from fastapi import Depends, HTTPException, status

from app.deps import get_current_user
from app.models import Role, User


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != Role.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


def require_accounting(user: User = Depends(get_current_user)) -> User:
    """Admin OR accountant may read/post in the accounting section.

    Period open/close and system-account deactivation remain ADMIN-only and
    use `require_admin` (design §3.5). CASHIER has no accounting access.
    """
    if user.role not in (Role.ADMIN, Role.ACCOUNTANT):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Accounting access required",
        )
    return user
