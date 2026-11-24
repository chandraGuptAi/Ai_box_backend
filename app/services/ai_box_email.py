"""Server-to-server call to Ai_Box_Service ``POST /api/email/internal/draft``.

Ai_Box_Service owns the mailbox tokens and creates the draft in Gmail/Outlook; this backend only supplies the
finished email (HTML + text built from the report, PDF). Three credentials travel with the call:
- ``X-Ai-Box-Service-Token``: this backend is allowed to call Ai_Box_Service at all;
- ``X-Email-Internal-Key``: this backend may use the internal draft endpoint (browsers never have it);
- ``X-Shipment-Authorization``: the user's BigShip login, which Ai_Box_Service checks with BigShip to learn
  which seller is asking, and therefore which mailboxes they may use.
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

from app.errors import AppError

#: Tests replace this with an httpx.MockTransport.
TRANSPORT: httpx.AsyncBaseTransport | None = None

NOT_CONFIGURED = "Email is not configured on this server yet."


async def create_draft(metadata: dict[str, Any], pdf: tuple[str, bytes] | None, shipment_authorization: str,
                       user_id: str) -> dict[str, Any]:
    base = os.getenv("AI_BOX_SERVICE_URL", "").strip().rstrip("/")
    token = os.getenv("AI_BOX_SERVICE_TOKEN", "").strip()
    key = os.getenv("AI_BOX_EMAIL_INTERNAL_KEY", "").strip()
    if not base or not key:
        raise AppError(NOT_CONFIGURED, code="email_not_configured", status_code=503)
    headers = {"X-Email-Internal-Key": key, "X-Shipment-Authorization": shipment_authorization,
               "X-User-Id": str(user_id)}
    if token:
        headers["X-Ai-Box-Service-Token"] = token
    files = {"attachment": (pdf[0], pdf[1], "application/pdf")} if pdf else None
    timeout = float(os.getenv("AI_BOX_EMAIL_TIMEOUT_SECONDS", "90"))
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0), transport=TRANSPORT,
                                     trust_env=False) as client:
            res = await client.post(f"{base}/api/email/internal/draft", headers=headers,
                                    data={"metadata": json.dumps(metadata)}, files=files)
    except httpx.HTTPError:
        raise AppError("The email service is not responding. Please try again.",
                       code="email_service_unavailable", status_code=503, retryable=True) from None
    if res.status_code == 200:
        return res.json()
    raise _upstream_error(res)


def _upstream_error(res: httpx.Response) -> AppError:
    """Ai_Box_Service answers ``{"detail": {"code", "message", "retryable", ...}}``; keep its code and message."""
    try:
        detail = res.json().get("detail")
    except ValueError:
        detail = None
    if not isinstance(detail, dict) or "code" not in detail:
        if res.status_code == 403:   # service token or internal key mismatch between the two services
            return AppError(NOT_CONFIGURED, code="email_not_configured", status_code=503)
        return AppError("Could not create the email. Please try again.", code="email_service_error",
                        status_code=502, retryable=True)
    extra = {k: detail[k] for k in ("action", "provider", "retry_after_seconds", "details") if k in detail}
    return AppError(str(detail.get("message") or "Could not create the email."), code=str(detail["code"]).lower(),
                    status_code=res.status_code, retryable=bool(detail.get("retryable")), details=extra or None)
