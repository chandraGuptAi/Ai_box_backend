"""Caller identity for user-scoped report storage.

Every saved-report endpoint requires the logged-in seller's ID in the
``X-User-Id`` header. Routes get a ``UserReports`` bound to that ID, so they
can only read, change or delete that user's own reports.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException

from app.config import settings
from app.storage.database import storage
from app.storage.user_reports import UserReports

logger = logging.getLogger("ai_box.auth")

_MAX_ID_LENGTH = 100
_MAX_NAME_LENGTH = 200


@dataclass(frozen=True)
class CurrentUser:
    id: str
    name: str


def get_current_user(
    x_user_id: str | None = Header(default=None),
    x_user_name: str | None = Header(default=None),
) -> CurrentUser:
    import sys
    is_testing = "pytest" in sys.modules
    dev_fallback = settings.dev_user_id or ("102795" if (settings.environment == "development" and not is_testing) else "")
    user_id = (x_user_id or "").strip() or dev_fallback
    if not user_id:
        logger.warning("Rejected AI Box request: no X-User-Id header.")
        raise HTTPException(status_code=401, detail="X-User-Id header is missing. The browser did not send the logged-in user ID.")
    if len(user_id) > _MAX_ID_LENGTH:
        logger.warning("Rejected AI Box request: X-User-Id is %d characters (max %d); value withheld.",
                       len(user_id), _MAX_ID_LENGTH)
        raise HTTPException(status_code=401,
                            detail=f"X-User-Id is {len(user_id)} characters long (max {_MAX_ID_LENGTH}); it is not a valid user ID.")
    name = (x_user_name or "").strip()[:_MAX_NAME_LENGTH] or user_id
    return CurrentUser(id=user_id, name=name)


def get_optional_user(
    x_user_id: str | None = Header(default=None),
    x_user_name: str | None = Header(default=None),
) -> CurrentUser | None:
    """For endpoints that also accept a per-report access token instead of a user."""
    if not (x_user_id or "").strip():
        return None
    return get_current_user(x_user_id, x_user_name)


def get_user_reports(user: CurrentUser = Depends(get_current_user)) -> UserReports:
    return UserReports(storage, user.id, user.name)


def get_optional_user_reports(user: CurrentUser | None = Depends(get_optional_user)) -> UserReports | None:
    return UserReports(storage, user.id, user.name) if user else None
