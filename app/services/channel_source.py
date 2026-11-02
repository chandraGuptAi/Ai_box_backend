"""Backend-owned channel-order fetch (``source="channel_api"``), server to server from Ai_Box_Service.

``GET {AI_BOX_SERVICE_URL}/api/channel_wise_report/channels/{channel_id}/orders?from_date=&to_date=`` returns
every store order of one sales channel whose order date (IST) falls in the selected days. Ai_Box_Service reads
Bigship ``/api/channel/orders/list`` with the caller's delegated BigShip token and leaves out customer name,
phone, email and street address. The browser never fetches or forwards these orders.

The same AI_BOX_SERVICE_URL / AI_BOX_SERVICE_TOKEN as the api-gateway route are used; the token goes in
``X-Ai-Box-Service-Token`` so Ai_Box_Service accepts the call.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from app.errors import (ShipmentSourceNotConfiguredError, SourceTooLargeError, UpstreamAuthError,
                        UpstreamInvalidResponseError, UpstreamRejectedError, UpstreamTimeoutError,
                        UpstreamUnavailableError)

SERVICE_TOKEN_HEADER = "X-Ai-Box-Service-Token"


@dataclass(frozen=True)
class ChannelOrders:
    rows: list[dict[str, Any]]
    summary: dict[str, Any]


class ChannelSource:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def fetch(self, channel_id: int, from_date: date, to_date: date, credential: str) -> ChannelOrders:
        """``credential``: the validated ``Bearer <BigShip token>`` from delegated_credential()."""
        base_url = os.getenv("AI_BOX_SERVICE_URL", "").strip().rstrip("/")
        if not base_url:
            raise ShipmentSourceNotConfiguredError("Channel reports are not configured (AI_BOX_SERVICE_URL).")
        timeout = float(os.getenv("AI_BOX_SERVICE_TIMEOUT_SECONDS", "60") or 60)
        headers = {"X-Shipment-Authorization": credential}
        token = os.getenv("AI_BOX_SERVICE_TOKEN", "").strip()
        if token:
            headers[SERVICE_TOKEN_HEADER] = token

        url = f"{base_url}/api/channel_wise_report/channels/{int(channel_id)}/orders"
        params = {"from_date": from_date.isoformat(), "to_date": to_date.isoformat(), "status": "all"}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0), trust_env=False,
                                         transport=self._transport) as client:
                res = await client.get(url, params=params, headers=headers)
        except httpx.TimeoutException:
            raise UpstreamTimeoutError("The channel order service timed out.") from None
        except httpx.RequestError:
            raise UpstreamUnavailableError("The channel order service is unavailable.") from None

        if res.status_code == 401:
            raise UpstreamAuthError("Your BigShip session was rejected. Log in again and retry.")
        if res.status_code == 413:
            raise SourceTooLargeError("This channel has too many orders for one report. Choose a shorter period.")
        if res.status_code >= 400:
            detail = _detail(res)
            raise UpstreamRejectedError(f"The channel order service refused the request: {detail}",
                                        details={"status": res.status_code})
        try:
            body = res.json()
            rows = body["orders"]
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            raise UpstreamInvalidResponseError("The channel order service returned an invalid response.") from None
        return ChannelOrders(rows=rows, summary=body.get("summary") or {})


def _detail(res: httpx.Response) -> str:
    try:
        detail = res.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    return str(detail)[:200] if detail else f"HTTP {res.status_code}"
