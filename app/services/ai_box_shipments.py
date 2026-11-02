"""Shipments fetched by Ai_Box_Service (``POST {AI_BOX_SERVICE_URL}/api/shipments/fetch``), server to server.

Ai_Box_Service reads every matching row from the Bigship order-list API with the caller's delegated token,
removes personal fields (names, contacts, addresses, documents, file links) and splits the lane into
origin/destination zones. The filters are still validated here (build_query), and the completeness gate
still runs on the result. Upstream error codes come back unchanged and are raised as this backend's errors.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from app.config import Settings, settings
from app.errors import (AppError, ShipmentSourceNotConfiguredError, UpstreamInvalidResponseError,
                        UpstreamTimeoutError, UpstreamUnavailableError)
from app.services.shipment_source import FetchResult, SourceQuery

SERVICE_TOKEN_HEADER = "X-Ai-Box-Service-Token"


def default_shipment_source(config: Settings = settings):
    """Ai_Box_Service when it is configured (SHIPMENT_SOURCE=ai_box_service, the default); else the direct fetch."""
    from app.services.shipment_source import ShipmentSource
    mode = os.getenv("SHIPMENT_SOURCE", "ai_box_service").strip().lower()
    if mode == "ai_box_service" and os.getenv("AI_BOX_SERVICE_URL", "").strip():
        return AiBoxShipmentSource(config)
    return ShipmentSource(config)


def _error_classes() -> dict[str, type[AppError]]:
    classes, pending = {}, [AppError]
    while pending:
        cls = pending.pop()
        classes.setdefault(cls.code, cls)
        pending.extend(cls.__subclasses__())
    return classes


class AiBoxShipmentSource:
    def __init__(self, config: Settings = settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = config
        self._transport = transport

    async def fetch(self, query: SourceQuery, credential: str) -> FetchResult:
        base_url = os.getenv("AI_BOX_SERVICE_URL", "").strip().rstrip("/")
        if not base_url:
            raise ShipmentSourceNotConfiguredError("Shipment reports are not available: the shipment service is "
                                                   "not configured on the server.")
        headers = {"X-Shipment-Authorization": credential}
        if token := os.getenv("AI_BOX_SERVICE_TOKEN", "").strip():
            headers[SERVICE_TOKEN_HEADER] = token
        cfg = self._settings
        body = {"status_type": query.status_type, "from_date": query.from_date, "to_date": query.to_date,
                "date_filter_type": query.date_filter_type, "segment_type": query.segment_type,
                "max_records": min(cfg.shipment_api_max_records, cfg.max_records)}
        try:
            # Ai_Box_Service has its own total deadline; this one only bounds a hung connection.
            async with httpx.AsyncClient(timeout=httpx.Timeout(cfg.shipment_api_total_timeout + 15, connect=5.0),
                                         trust_env=False, transport=self._transport) as client:
                res = await client.post(f"{base_url}/api/shipments/fetch", json=body, headers=headers)
        except httpx.TimeoutException:
            raise UpstreamTimeoutError("The shipment service took too long to respond. Please try again.") from None
        except httpx.RequestError:
            raise UpstreamUnavailableError("The shipment service could not be reached. Please try again.") from None
        if res.status_code >= 400:
            raise self._error(res)
        try:
            data = res.json()
            rows = data["rows"]
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows) or len(rows) != data["total"]:
                raise TypeError
            return FetchResult(rows=rows, total=int(data["total"]), account_summary=dict(data.get("account_summary") or {}),
                               fetched_at=str(data["fetched_at"]), requests=int(data["requests"]),
                               page_size=int(data["page_size"]), pages=int(data["pages"]),
                               attempts=int(data["attempts"]), page_size_clamped=bool(data["page_size_clamped"]),
                               query=query.describe(), removed_fields=list(data.get("removed_fields") or []),
                               via="ai_box_service", cached=bool(data.get("cached")))
        except (ValueError, KeyError, TypeError):
            raise UpstreamInvalidResponseError("The shipment service returned an unreadable response.") from None

    @staticmethod
    def _error(res: httpx.Response) -> AppError:
        try:
            detail: Any = res.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        if isinstance(detail, dict) and isinstance(detail.get("code"), str):
            cls = _error_classes().get(detail["code"])
            if cls is not None:
                return cls(str(detail.get("message") or "The shipment service could not return the shipments."),
                           details=detail.get("details"), status_code=res.status_code,
                           retryable=bool(detail.get("retryable")))
        if res.status_code == 403:
            # Ai_Box_Service refused this backend's service token: a deployment setting, not the seller's session.
            return ShipmentSourceNotConfiguredError("Shipment reports are not available: the shipment service "
                                                    "rejected this server's credentials.")
        if res.status_code == 401:
            from app.errors import UpstreamAuthError
            return UpstreamAuthError("Your shipment service session was rejected. Log in again and retry.")
        return UpstreamUnavailableError("The shipment service is unavailable. Please try again.",
                                        details={"status": res.status_code})
