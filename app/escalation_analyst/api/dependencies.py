"""Reuse host identity; internal bridge access requires an independent shared secret."""

from __future__ import annotations

import hmac
from fastapi import Depends, Header, Request
from app.api.auth import CurrentUser, get_current_user
from ..errors import EscalationError


def runtime(request: Request):
    return request.app.state.runtime


def user(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    return current


def internal(request: Request, x_sidecar_secret: str | None = Header(default=None)) -> None:
    secret = request.app.state.runtime.settings.sidecar_secret
    if not secret or not hmac.compare_digest(secret, x_sidecar_secret or ""):
        raise EscalationError(401, "Invalid sidecar credentials")
