from fastapi import Depends, HTTPException, status

from app.deps import get_current_user
from app.models import Role, User


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != Role.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user
