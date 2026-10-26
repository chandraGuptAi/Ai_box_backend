"""Central HTTP gateway. Business logic lives in independently deployed services."""
import os
import math
from dataclasses import dataclass
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI
from starlette.responses import JSONResponse, StreamingResponse


HOP_HEADERS = {b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization",
               b"te", b"trailer", b"transfer-encoding", b"upgrade", b"host"}


def filtered_headers(raw):
    connection_tokens = {token.strip().lower() for key, value in raw if key.lower() == b"connection"
                         for token in value.split(b",")}
    return [(key, value) for key, value in raw if key.lower() not in HOP_HEADERS | connection_tokens]


class ServiceGateway:
    def __init__(self, base_url, token, timeout, client=None, *, name="Service", token_header="X-Escalation-Service-Token"):
        self.name = name
        self.token_header = token_header.lower().encode("ascii")
        self.base_url, self.token, self.timeout = base_url.rstrip("/"), token, timeout
        self.client = client

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        if not self.base_url:
            return await JSONResponse({"error": f"{self.name} service is not configured"}, 503)(scope, receive, send)
        if self.client is None:
            return await JSONResponse({"error": f"{self.name} gateway is not ready"}, 503)(scope, receive, send)
        # Preserve encoded path/query exactly, including repeated query parameters.
        raw_path = scope.get("raw_path", scope["path"].encode()).split(b"?", 1)[0]
        url = httpx.URL(self.base_url).copy_with(raw_path=raw_path +
                     (b"?" + scope["query_string"] if scope.get("query_string") else b""))
        headers = [(k, v) for k, v in filtered_headers(scope["headers"])
                   if k.lower() != self.token_header]
        if self.token:
            headers.append((self.token_header, self.token.encode()))

        async def body():
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    raise httpx.ReadError("Caller disconnected")
                if message.get("body"):
                    yield message["body"]
                if not message.get("more_body", False):
                    break

        try:
            request = self.client.build_request(scope["method"], url, headers=headers, content=body(), timeout=self.timeout)
            upstream = await self.client.send(request, stream=True, follow_redirects=False)
        except httpx.TimeoutException:
            return await JSONResponse({"error": f"{self.name} service timed out"}, 504)(scope, receive, send)
        except httpx.RequestError:
            return await JSONResponse({"error": f"{self.name} service unavailable"}, 502)(scope, receive, send)

        async def stream():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        response = StreamingResponse(stream(), status_code=upstream.status_code)
        response.raw_headers = filtered_headers(upstream.headers.raw)
        # Rewrite private redirects to the original public path.
        response.raw_headers = [(k, v.replace(self.base_url.encode(), b"", 1)
                                 if k.lower() == b"location" and v.startswith(self.base_url.encode() + b"/") else v)
                                for k, v in response.raw_headers]
        try:
            await response(scope, receive, send)
        finally:
            await upstream.aclose()



@dataclass(frozen=True)
class ServiceRoute:
    name: str
    prefix: str
    url_env: str
    token_env: str
    timeout_env: str
    token_header: str = "X-Escalation-Service-Token"
    default_timeout: float = 1800


# Add a ServiceRoute here for each independently deployed service.
# Upstreams receive the full public path, including the registered prefix.
SERVICE_ROUTES = (
    ServiceRoute("WhatsApp", "/api/escalation", "ESCALATION_SERVICE_URL",
                 "ESCALATION_SERVICE_TOKEN", "ESCALATION_GATEWAY_TIMEOUT_SECONDS"),
)


def install(host: FastAPI, routes=None):
    routes = SERVICE_ROUTES if routes is None else tuple(routes)
    gateways = {}
    for route in routes:
        if not route.prefix.startswith("/") or route.prefix == "/" or route.prefix.endswith("/") or route.prefix in gateways:
            raise ValueError("Service prefixes must be unique absolute paths without trailing slashes")
        base_url = os.getenv(route.url_env, "").strip().rstrip("/")
        token = os.getenv(route.token_env, "").strip()
        if base_url:
            parsed = urlsplit(base_url)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname
                    or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                    or parsed.username is not None):
                raise ValueError(f"{route.url_env} must be an HTTP(S) origin")
            if not token:
                raise ValueError(f"{route.token_env} is required when {route.url_env} is configured")
        seconds = float(os.getenv(route.timeout_env, str(route.default_timeout)))
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError(f"{route.timeout_env} must be a positive finite number")
        gateways[route.prefix] = ServiceGateway(
            base_url, token, httpx.Timeout(seconds, connect=5.0),
            name=route.name, token_header=route.token_header)
    previous_lifespan = host.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with previous_lifespan(app) as state:
            async with httpx.AsyncClient(trust_env=False) as client:
                for gateway in gateways.values():
                    gateway.client = client
                try:
                    yield state
                finally:
                    for gateway in gateways.values():
                        gateway.client = None

    host.router.lifespan_context = lifespan
    # More specific prefixes must be mounted first.
    for prefix in sorted(gateways, key=len, reverse=True):
        host.mount(prefix, gateways[prefix], name=gateways[prefix].name.lower() + "_gateway")
    return gateways
