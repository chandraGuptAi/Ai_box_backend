"""Synced WhatsApp Business / Instagram / Facebook Page metrics, server to server from Ai_Box_Service.

``POST {AI_BOX_SERVICE_URL}/api/meta/report-context`` returns the metrics Ai_Box_Service's background sync
stored for the seller's connected Meta accounts in the selected days. It never calls Meta and never invents
values: missing data comes back as ``unavailable_metrics`` / ``status: no_data``.

Ai_Box_Service identifies the seller itself, by asking BigShip whose login token
(``X-Shipment-Authorization``) the request carries; ``X-User-Id`` is only used in its local-development
mode. The same AI_BOX_SERVICE_URL / AI_BOX_SERVICE_TOKEN as the api-gateway route are used.
"""
from __future__ import annotations

import os
from datetime import date
from typing import Any

import httpx

from app.errors import (MetaSourceNotConfiguredError, ShipmentSourceNotConfiguredError, UpstreamAuthError,
                        UpstreamInvalidResponseError, UpstreamRejectedError, UpstreamTimeoutError,
                        UpstreamUnavailableError)

SERVICE_TOKEN_HEADER = "X-Ai-Box-Service-Token"
META_SOURCES = ("whatsapp", "instagram", "facebook")


class MetaSource:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def context(self, sources: list[str], from_date: date, to_date: date, timezone: str,
                      credential: str | None, user_id: str | None) -> dict[str, Any]:
        base_url = os.getenv("AI_BOX_SERVICE_URL", "").strip().rstrip("/")
        if not base_url:
            raise ShipmentSourceNotConfiguredError(
                "WhatsApp, Instagram and Facebook reports are not configured (AI_BOX_SERVICE_URL).")
        timeout = float(os.getenv("AI_BOX_SERVICE_TIMEOUT_SECONDS", "60") or 60)
        headers = {}
        if credential:
            headers["X-Shipment-Authorization"] = credential
        if user_id:
            headers["X-User-Id"] = str(user_id)
        token = os.getenv("AI_BOX_SERVICE_TOKEN", "").strip()
        if token:
            headers[SERVICE_TOKEN_HEADER] = token
        body = {"data_sources": list(sources), "from_date": from_date.isoformat(), "to_date": to_date.isoformat(),
                "timezone": timezone}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0), trust_env=False,
                                         transport=self._transport) as client:
                res = await client.post(f"{base_url}/api/meta/report-context", json=body, headers=headers)
        except httpx.TimeoutException:
            raise UpstreamTimeoutError("The social media data service timed out.") from None
        except httpx.RequestError:
            raise UpstreamUnavailableError("The social media data service is unavailable.") from None

        if res.status_code >= 400:
            detail = _detail(res)
            code = detail.get("code")
            if code == "META_SELLER_UNAUTHENTICATED" or res.status_code == 401:
                raise UpstreamAuthError("Your BigShip session was rejected. Log in again and retry.")
            if code == "META_PLATFORM_NOT_CONFIGURED":
                raise MetaSourceNotConfiguredError(
                    "WhatsApp, Instagram and Facebook connections are not configured on the server yet.")
            if code == "META_IDENTITY_UNAVAILABLE":
                raise UpstreamUnavailableError("Your BigShip login cannot be verified right now. Please retry.")
            raise UpstreamRejectedError("The social media data service refused the request.",
                                        details={"status": res.status_code, "meta_code": code})
        try:
            context = res.json()
            if not isinstance(context.get("sources"), dict):
                raise TypeError
        except (ValueError, AttributeError, TypeError):
            raise UpstreamInvalidResponseError("The social media data service returned an invalid response.") from None
        return context


def _detail(res: httpx.Response) -> dict[str, Any]:
    try:
        detail = res.json().get("detail")
    except (ValueError, AttributeError):
        return {}
    return detail if isinstance(detail, dict) else {}
