"""ASGI middleware that rejects request bodies larger than the configured limit."""

from __future__ import annotations

import json

from fastapi import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.errors import error_payload


class RequestTooLargeError(HTTPException):
    """Raised while streaming a body without Content-Length that exceeds the limit.

    It subclasses FastAPI's HTTPException so FastAPI's body parsing re-raises it
    unchanged and the normal exception handlers turn it into a 413 response.
    """

    def __init__(self, max_bytes: int) -> None:
        super().__init__(
            status_code=413,
            detail=f"Request body exceeds the maximum allowed size of {max_bytes} bytes.",
        )


class RequestSizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return

        content_length = _header(scope, b"content-length")
        if content_length is not None:
            if not content_length.isdigit():
                await _send_error(send, 400, "invalid_content_length", "Invalid Content-Length header.")
                return
            if int(content_length) > self.max_bytes:
                await _send_error(
                    send,
                    413,
                    "request_too_large",
                    f"Request body exceeds the maximum allowed size of {self.max_bytes} bytes.",
                )
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise RequestTooLargeError(self.max_bytes)
            return message

        await self.app(scope, limited_receive, send)


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1").strip()
    return None


async def _send_error(send: Send, status: int, code: str, message: str) -> None:
    body = json.dumps(error_payload(code, "validation", message)).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
