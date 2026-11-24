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
    def __init__(self, base_url, token, timeout, client=None, *, name="Service", token_header="X-Escalation-Service-Token",
                 blocked_paths=()):
        self.name = name
        # Upstream paths (relative to the mount prefix) that callers may never reach through the gateway.
        self.blocked_paths = tuple(blocked_paths)
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
        if self.blocked_paths:
            path, root = scope["path"], scope.get("root_path", "")
            relative = path[len(root):] if root and path.startswith(root) else path
            if any(relative == p or relative.startswith(p + "/") for p in self.blocked_paths):
                return await JSONResponse({"error": "Not found"}, 404)(scope, receive, send)
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
    blocked_paths: tuple = ()


# Add a ServiceRoute here for each independently deployed service.
# Upstreams receive the full public path, including the registered prefix.
SERVICE_ROUTES = (
    ServiceRoute("WhatsApp", "/api/escalation", "ESCALATION_SERVICE_URL",
                 "ESCALATION_SERVICE_TOKEN", "ESCALATION_GATEWAY_TIMEOUT_SECONDS"),
    # Ai_Box_Service module; it forwards the seller's X-Shipment-Authorization token to Bigship.
    ServiceRoute("ChannelWiseReport", "/api/channel_wise_report", "AI_BOX_SERVICE_URL",
                 "AI_BOX_SERVICE_TOKEN", "AI_BOX_SERVICE_TIMEOUT_SECONDS",
                 token_header="X-Ai-Box-Service-Token", default_timeout=60),
    # Ai_Box_Service module: AI calling (Voice AI) call logs; it checks the caller's X-Audit-Key itself.
    ServiceRoute("AuditLog", "/api/audit_log", "AI_BOX_SERVICE_URL",
                 "AI_BOX_SERVICE_TOKEN", "AI_BOX_SERVICE_TIMEOUT_SECONDS",
                 token_header="X-Ai-Box-Service-Token", default_timeout=60),
    # Ai_Box_Service module: sellers' WhatsApp Business / Instagram / Facebook Page connections and insights.
    # Seller routes are identified there by asking BigShip whose X-Shipment-Authorization login it is (never
    # by X-User-Id); Meta's own callbacks (/oauth/callback, /webhook, /deauthorize, /data-deletion) are
    # authenticated by OAuth state or Meta signatures. The streaming proxy forwards the raw body unchanged,
    # which webhook signature checks require.
    ServiceRoute("MetaIntegration", "/api/meta", "AI_BOX_SERVICE_URL",
                 "AI_BOX_SERVICE_TOKEN", "AI_BOX_SERVICE_TIMEOUT_SECONDS",
                 token_header="X-Ai-Box-Service-Token", default_timeout=60),
    # Ai_Box_Service module: sellers' Gmail / Outlook mailboxes (connect, list, disconnect, OAuth callback, send
    # history). Sending itself goes through POST /api/reports/{id}/email/send here, which builds the body and PDF
    # and calls /api/email/internal/send server-to-server; browsers can never reach /internal/* via the gateway.
    ServiceRoute("EmailIntegration", "/api/email", "AI_BOX_SERVICE_URL",
                 "AI_BOX_SERVICE_TOKEN", "AI_BOX_SERVICE_TIMEOUT_SECONDS",
                 token_header="X-Ai-Box-Service-Token", default_timeout=60, blocked_paths=("/internal",)),
    # Ai_Box_Erp service: ERP panel "AI Box" page (POST /api/ai-box-erp/generate, multipart in, PDF out).
    # Its own prefix, so it never overlaps this backend's /api/ai-insights/* routes. The LLM call runs
    # inside the request, hence the long timeout; app/middleware.py allows 51 MB bodies on this prefix.
    ServiceRoute("AiBoxErp", "/api/ai-box-erp", "AI_BOX_ERP_SERVICE_URL",
                 "AI_BOX_ERP_SERVICE_TOKEN", "AI_BOX_ERP_SERVICE_TIMEOUT_SECONDS",
                 token_header="X-Ai-Box-Service-Token", default_timeout=240),
    # Ai_Box_Service module ai_assistant: the report page's AI Assistant tab (emails, messages, medicine info).
    ServiceRoute("AiAssistant", "/api/assistant", "AI_BOX_SERVICE_URL",
                 "AI_BOX_SERVICE_TOKEN", "AI_BOX_SERVICE_TIMEOUT_SECONDS",
                 token_header="X-Ai-Box-Service-Token", default_timeout=60),
    # Ai_Box_Service module channel_insights: "AI Insights" box on the Channel Orders page.
    ServiceRoute("ChannelInsights", "/api/channel-insights", "AI_BOX_SERVICE_URL",
                 "AI_BOX_SERVICE_TOKEN", "AI_BOX_SERVICE_TIMEOUT_SECONDS",
                 token_header="X-Ai-Box-Service-Token", default_timeout=60),
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
            name=route.name, token_header=route.token_header, blocked_paths=route.blocked_paths)
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
