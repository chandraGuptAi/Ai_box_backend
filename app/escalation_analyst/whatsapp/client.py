"""Only Python file permitted to call the Node WhatsApp transport."""

from __future__ import annotations

import httpx
from ..errors import EscalationError


class Sidecar:
    def __init__(self, settings):
        self.settings = settings

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        if not self.settings.sidecar_secret:
            raise EscalationError(503, "WhatsApp sidecar secret is not configured")
        try:
            response = httpx.request(
                method,
                self.settings.sidecar_url + path,
                json=body,
                headers={"X-Sidecar-Secret": self.settings.sidecar_secret},
                timeout=15,
                trust_env=False,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EscalationError(
                503, "WhatsApp connection unavailable; reconnect or retry shortly."
            ) from exc

    def status(self) -> dict:
        try:
            result = self.call("GET", "/status")
            aliases = {
                "connecting": "initializing",
                "qr": "qr_required",
                "waiting_for_qr": "qr_required",
                "auth_failure": "error",
            }
            result["state"] = aliases.get(result.get("state"), result.get("state", "error"))
            return result
        except EscalationError as exc:
            return dict(state="disconnected", qrImage=None, expiresAt=None, message=exc.message, demo=False)

    def send(self, recipient: str, text: str) -> dict:
        return self.call("POST", "/send", {"to": recipient, "text": text})
