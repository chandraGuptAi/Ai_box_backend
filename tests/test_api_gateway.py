import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api_gateway import ServiceRoute, install


def test_disabled_service_does_not_break_host(monkeypatch):
    monkeypatch.delenv("ESCALATION_SERVICE_URL", raising=False)
    app = FastAPI()
    install(app)
    with TestClient(app) as client:
        assert client.get("/api/escalation/groups").status_code == 503


def test_multiple_services_preserve_contract(monkeypatch):
    routes = [ServiceRoute("WhatsApp", "/api/escalation", "TEST_WA_URL", "TEST_WA_TOKEN", "TEST_TIMEOUT"),
              ServiceRoute("Other", "/api/other", "TEST_OTHER_URL", "TEST_OTHER_TOKEN", "TEST_TIMEOUT", "X-Other-Token")]
    monkeypatch.setenv("TEST_WA_URL", "http://whatsapp:8201")
    monkeypatch.setenv("TEST_WA_TOKEN", "wa-secret")
    monkeypatch.setenv("TEST_OTHER_URL", "http://other:8202")
    monkeypatch.setenv("TEST_OTHER_TOKEN", "other-secret")
    seen = []

    async def upstream(request):
        seen.append((request, await request.aread()))
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"export"
        return httpx.Response(200, stream=Stream(), headers={"Content-Disposition": "attachment; filename=test.xlsx"})

    app = FastAPI()
    gateways = install(app, routes)
    with TestClient(app) as client:
        transport_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        for gateway in gateways.values():
            gateway.client = transport_client
        response = client.post("/api/escalation/groups/a%40g.us?x=1&x=2", content=b"upload",
                               headers={"X-User-Id": "user", "X-Escalation-Service-Token": "forged"})
        assert response.content == b"export"
        assert response.headers["content-disposition"].endswith("test.xlsx")
        assert client.get("/api/other/items").status_code == 200
        client.portal.call(transport_client.aclose)
    assert seen[0][0].url.raw_path == b"/api/escalation/groups/a%40g.us?x=1&x=2"
    assert seen[0][1] == b"upload"
    assert seen[0][0].headers["x-escalation-service-token"] == "wa-secret"
    assert seen[0][0].headers["x-user-id"] == "user"
    assert seen[1][0].url.host == "other"
    assert seen[1][0].headers["x-other-token"] == "other-secret"


@pytest.mark.parametrize("error,status", [(httpx.ConnectError("offline"), 502), (httpx.ReadTimeout("timeout"), 504)])
def test_upstream_failure(monkeypatch, error, status):
    monkeypatch.setenv("ESCALATION_SERVICE_URL", "http://whatsapp:8201")
    monkeypatch.setenv("ESCALATION_SERVICE_TOKEN", "secret")
    async def upstream(request):
        raise error
    app = FastAPI()
    gateways = install(app)
    with TestClient(app) as client:
        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        gateways["/api/escalation"].client = upstream_client
        assert client.get("/api/escalation/groups").status_code == status
        client.portal.call(upstream_client.aclose)


def test_configured_service_requires_token(monkeypatch):
    monkeypatch.setenv("ESCALATION_SERVICE_URL", "http://whatsapp:8201")
    monkeypatch.delenv("ESCALATION_SERVICE_TOKEN", raising=False)
    with pytest.raises(ValueError, match="TOKEN"):
        install(FastAPI())


def test_channel_wise_report_route_reaches_ai_box_service(monkeypatch):
    monkeypatch.setenv("AI_BOX_SERVICE_URL", "http://ai-box:8100")
    monkeypatch.setenv("AI_BOX_SERVICE_TOKEN", "box-secret")
    seen = []

    async def upstream(request):
        seen.append(request)
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{"channels": []}'
        return httpx.Response(200, stream=Stream(), headers={"Content-Type": "application/json"})

    app = FastAPI()
    gateways = install(app)
    with TestClient(app) as client:
        transport_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        for gateway in gateways.values():
            gateway.client = transport_client
        response = client.get("/api/channel_wise_report/channels",
                              headers={"X-Shipment-Authorization": "Bearer seller-token",
                                       "X-Ai-Box-Service-Token": "forged"})
        client.portal.call(transport_client.aclose)
    assert response.status_code == 200
    assert str(seen[0].url) == "http://ai-box:8100/api/channel_wise_report/channels"
    assert seen[0].headers["x-shipment-authorization"] == "Bearer seller-token"
    assert seen[0].headers["x-ai-box-service-token"] == "box-secret"


def test_audit_log_route_reaches_ai_box_service_with_the_callers_audit_key(monkeypatch):
    monkeypatch.setenv("AI_BOX_SERVICE_URL", "http://ai-box:8100")
    monkeypatch.setenv("AI_BOX_SERVICE_TOKEN", "box-secret")
    seen = []

    async def upstream(request):
        seen.append(request)
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{"total": 0, "calls": []}'
        return httpx.Response(200, stream=Stream(), headers={"Content-Type": "application/json"})

    app = FastAPI()
    gateways = install(app)
    with TestClient(app) as client:
        transport_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        for gateway in gateways.values():
            gateway.client = transport_client
        response = client.get("/api/audit_log/call-logs?limit=10", headers={"X-Audit-Key": "admin-key"})
        client.portal.call(transport_client.aclose)
    assert response.status_code == 200
    assert str(seen[0].url) == "http://ai-box:8100/api/audit_log/call-logs?limit=10"
    assert seen[0].headers["x-audit-key"] == "admin-key"
    assert seen[0].headers["x-ai-box-service-token"] == "box-secret"


def test_meta_route_reaches_ai_box_service_with_raw_body_and_seller(monkeypatch):
    monkeypatch.setenv("AI_BOX_SERVICE_URL", "http://ai-box:8100")
    monkeypatch.setenv("AI_BOX_SERVICE_TOKEN", "box-secret")
    seen = []

    async def upstream(request):
        seen.append((request, await request.aread()))
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{"received": 1, "stored": 1}'
        return httpx.Response(200, stream=Stream(), headers={"Content-Type": "application/json"})

    app = FastAPI()
    gateways = install(app)
    raw = b'{"object":"whatsapp_business_account",  "entry":[]}'   # spacing must survive for HMAC
    with TestClient(app) as client:
        transport_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        for gateway in gateways.values():
            gateway.client = transport_client
        hook = client.post("/api/meta/webhook", content=raw,
                           headers={"X-Hub-Signature-256": "sha256=abc", "X-Ai-Box-Service-Token": "forged"})
        conns = client.get("/api/meta/connections", headers={"X-User-Id": "102795"})
        client.portal.call(transport_client.aclose)
    assert hook.status_code == 200 and conns.status_code == 200
    request, body = seen[0]
    assert str(request.url) == "http://ai-box:8100/api/meta/webhook" and body == raw
    assert request.headers["x-hub-signature-256"] == "sha256=abc"
    assert request.headers["x-ai-box-service-token"] == "box-secret"   # caller's value replaced
    assert seen[1][0].headers["x-user-id"] == "102795"


def test_ai_box_erp_route_reaches_ai_box_erp_service_only(monkeypatch):
    monkeypatch.setenv("AI_BOX_ERP_SERVICE_URL", "http://ai-box-erp:8200")
    monkeypatch.setenv("AI_BOX_ERP_SERVICE_TOKEN", "erp-secret")
    seen = []

    async def upstream(request):
        seen.append((request, await request.aread()))
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{"status": true, "ReportHistoryIds": ["1"]}'
        return httpx.Response(200, stream=Stream(), headers={"Content-Type": "application/json"})

    app = FastAPI()

    @app.get("/api/ai-insights/overview")
    def overview():
        return {"local": True}

    gateways = install(app)
    with TestClient(app) as client:
        transport_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        for gateway in gateways.values():
            gateway.client = transport_client
        generated = client.post("/api/ai-box-erp/generate", json={"prompt": "x"}, follow_redirects=False,
                                headers={"Authorization": "Bearer erp-user", "X-Ai-Box-Service-Token": "forged"})
        local = client.get("/api/ai-insights/overview")
        old = client.post("/api/ai-insights/generate", json={"prompt": "x"}, follow_redirects=False)
        client.portal.call(transport_client.aclose)
    assert generated.status_code == 200 and generated.json()["ReportHistoryIds"] == ["1"]
    assert local.json() == {"local": True}            # /api/ai-insights/* stays on this backend
    assert old.status_code in (404, 405)              # the old path is no longer forwarded
    request, body = seen[0]
    assert len(seen) == 1
    assert str(request.url) == "http://ai-box-erp:8200/api/ai-box-erp/generate" and body == b'{"prompt":"x"}'
    assert request.headers["authorization"] == "Bearer erp-user"
    assert request.headers["x-ai-box-service-token"] == "erp-secret"


def test_ai_box_erp_multipart_upload_and_pdf_pass_through_unchanged(monkeypatch):
    from app.middleware import RequestSizeLimitMiddleware
    monkeypatch.setenv("AI_BOX_ERP_SERVICE_URL", "http://ai-box-erp:8200")
    monkeypatch.setenv("AI_BOX_ERP_SERVICE_TOKEN", "erp-secret")
    pdf = b"%PDF-1.4\n" + bytes(range(256)) * 400 + b"\n%%EOF"      # binary, not valid UTF-8
    seen = []

    async def upstream(request):
        seen.append((request, await request.aread()))
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for i in range(0, len(pdf), 8192):
                    yield pdf[i:i + 8192]
        return httpx.Response(200, stream=Stream(), headers={
            "Content-Type": "application/pdf",
            "Content-Disposition": 'attachment; filename="ai_insights_first_mile_report_21-11-2026 14-00-00.pdf"'})

    app = FastAPI()
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=10 * 1024 * 1024)
    gateways = install(app)
    report = b"PK\x03\x04" + b"\x00\xff" * (6 * 1024 * 1024)          # 12 MB: above the general 10 MB limit
    with TestClient(app) as client:
        transport_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        for gateway in gateways.values():
            gateway.client = transport_client
        response = client.post("/api/ai-box-erp/generate",
                               data={"request": '{"verticalId": "2", "ReportCategoryId": 7}'},
                               files={"file": ("Unified_FM_report_21-11-2026 13-30-07.xlsx", report,
                                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                               headers={"Authorization": "Bearer erp-user"})
        too_big = client.post("/api/ai-box-erp/generate", content=b"x" * (52 * 1024 * 1024),
                              headers={"Content-Type": "application/octet-stream"})
        elsewhere = client.post("/api/escalation/x", content=b"x" * (11 * 1024 * 1024))
        client.portal.call(transport_client.aclose)
    assert response.status_code == 200 and response.content == pdf
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].endswith('14-00-00.pdf"')
    request, body = seen[0]
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert report in body and b'name="request"' in body                  # multipart forwarded byte for byte
    assert len(seen) == 1
    assert too_big.status_code == 413 and elsewhere.status_code == 413   # other limits unchanged


def test_email_route_reaches_ai_box_service_but_never_its_internal_send(monkeypatch):
    monkeypatch.setenv("AI_BOX_SERVICE_URL", "http://ai-box:8100")
    monkeypatch.setenv("AI_BOX_SERVICE_TOKEN", "box-secret")
    seen = []

    async def upstream(request):
        seen.append(str(request.url))
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{"connections": []}'
        return httpx.Response(200, stream=Stream(), headers={"Content-Type": "application/json"})

    app = FastAPI()
    gateways = install(app)
    with TestClient(app) as client:
        transport_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        for gateway in gateways.values():
            gateway.client = transport_client
        ok = client.get("/api/email/connections", headers={"X-Shipment-Authorization": "Bearer 1|abcdefgh"})
        blocked = [client.post(path, headers={"X-Email-Internal-Key": "guess"}).status_code
                   for path in ("/api/email/internal/send", "/api/email/internal", "/api/email/%69nternal/send")]
        client.portal.call(transport_client.aclose)
    assert ok.status_code == 200 and seen == ["http://ai-box:8100/api/email/connections"]
    assert blocked == [404, 404, 404]


def test_ai_assistant_route_reaches_ai_box_service_with_the_service_token(monkeypatch):
    monkeypatch.setenv("AI_BOX_SERVICE_URL", "http://ai-box:8100")
    monkeypatch.setenv("AI_BOX_SERVICE_TOKEN", "box-secret")
    seen = []

    async def upstream(request):
        seen.append((request.method, str(request.url), request.headers.get("X-Ai-Box-Service-Token"),
                     request.headers.get("X-User-Id"), request.content))
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{"answer": "Subject: Sorry"}'
        return httpx.Response(200, stream=Stream(), headers={"Content-Type": "application/json"})

    app = FastAPI()
    gateways = install(app)
    with TestClient(app) as client:
        transport_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        for gateway in gateways.values():
            gateway.client = transport_client
        ok = client.post("/api/assistant/ask", json={"instruction": "sorry email"},
                         headers={"X-User-Id": "102795", "X-Ai-Box-Service-Token": "forged"})
        client.portal.call(transport_client.aclose)
    assert ok.status_code == 200 and ok.json() == {"answer": "Subject: Sorry"}
    assert seen == [("POST", "http://ai-box:8100/api/assistant/ask", "box-secret", "102795",
                     b'{"instruction":"sorry email"}')]

def test_channel_insights_route_reaches_ai_box_service_with_the_seller_token(monkeypatch):
    monkeypatch.setenv("AI_BOX_SERVICE_URL", "http://ai-box:8100")
    monkeypatch.setenv("AI_BOX_SERVICE_TOKEN", "box-secret")
    seen = []

    async def upstream(request):
        seen.append((str(request.url), request.headers.get("X-Ai-Box-Service-Token"),
                     request.headers.get("X-Shipment-Authorization")))
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{"intent": "ship"}'
        return httpx.Response(200, stream=Stream(), headers={"Content-Type": "application/json"})

    app = FastAPI()
    gateways = install(app)
    with TestClient(app) as client:
        transport_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        for gateway in gateways.values():
            gateway.client = transport_client
        ok = client.post("/api/channel-insights/ask", json={"question": "What should I ship today?"},
                         headers={"X-Shipment-Authorization": "Bearer 12|abc"})
        client.portal.call(transport_client.aclose)
    assert ok.status_code == 200 and ok.json() == {"intent": "ship"}
    assert seen == [("http://ai-box:8100/api/channel-insights/ask", "box-secret", "Bearer 12|abc")]
