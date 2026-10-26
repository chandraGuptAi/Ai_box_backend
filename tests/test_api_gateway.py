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
