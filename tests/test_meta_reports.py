"""WhatsApp / Instagram / Facebook reports (data_sources) from a mocked Ai_Box_Service /api/meta/report-context."""
from __future__ import annotations

import json
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.report_routes import get_report_service
from app.config import settings
from app.main import app
from app.models.report_schema import ReportRequest, ReportResponse
from app.services.channel_source import ChannelSource
from app.services.meta_source import MetaSource
from app.services.report_service import ReportService
from tests.test_channel_source import FILTERS as CHANNEL_FILTERS, channel_orders
from tests.test_dynamic_reports import NARRATIVE
from tests.test_reports import FakeAI, isolated_validation_audit  # noqa: F401  (fixture)

TOKEN = "Bearer seller-login-token-123456"
HEADERS = {"X-User-Id": "102795", "X-Shipment-Authorization": TOKEN}
TO_DAY = date.today() - timedelta(days=1)
FROM_DAY = TO_DAY - timedelta(days=2)
FILTERS = {"from_date": FROM_DAY.isoformat(), "to_date": TO_DAY.isoformat(), "timezone": "Asia/Kolkata"}
DAYS = [(FROM_DAY + timedelta(days=i)).isoformat() for i in range(3)]


def whatsapp_account(**overrides) -> dict:
    return {"connection_id": "c1", "account_name": "Acme Support WABA", "external_account_id": "1000000000000001",
            "connection_status": "connected", "reconnect_required": False,
            "last_successful_sync_at": f"{TO_DAY.isoformat()}T07:10:02Z", "data_stale": False,
            "coverage": {"first_date": DAYS[0], "last_date": DAYS[-1], "days_with_data": 3, "requested_days": 3,
                         "complete": True},
            "metrics": {
                "messages_sent": {"kind": "daily", "unit": "messages", "additive": True, "total": 1840,
                                  "days_with_data": 3, "daily": [{"date": d, "value": v}
                                                                 for d, v in zip(DAYS, (600, 640, 600))]},
                "pricing_volume": {"kind": "daily", "unit": "messages", "additive": True,
                                   "by_dimension": [{"dimension": "MARKETING", "total": 400, "days_with_data": 3}]}},
            "items": {},
            "unavailable_metrics": [{"metric": "template_read", "reason": "permission_required", "detail": "x"}],
            **overrides}


def context(**sources) -> dict:
    return {"seller_scope": "authenticated_seller",
            "requested_period": {"from_date": FROM_DAY.isoformat(), "to_date": TO_DAY.isoformat(),
                                 "timezone": "Asia/Kolkata"},
            "generated_at": f"{TO_DAY.isoformat()}T08:00:00Z", "sources": sources,
            "comparison_rules": ["Report each metric under its own source and account; never add values from "
                                 "different sources together."]}


OK = {"status": "ok", "accounts": [whatsapp_account()], "caveats": ["Counts are aggregates from Meta."]}
NOT_CONNECTED = {"status": "not_connected", "accounts": [],
                 "error": {"code": "META_NOT_CONNECTED", "action": "connect", "platform": "instagram"}}


class AiBox:
    """Ai_Box_Service: /api/meta/report-context and (for combined reports) channel orders."""

    def __init__(self, meta: dict | None = None, status: int = 200, detail: dict | None = None, rows=None):
        self.meta, self.status, self.detail, self.rows = meta, status, detail, rows or []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/meta/report-context":
            if self.status != 200:
                return httpx.Response(self.status, json={"detail": self.detail or {}})
            return httpx.Response(200, json=self.meta)
        return httpx.Response(200, json={"channel_id": 13, "status": "all", "total_records": len(self.rows),
                                         "summary": {}, "orders": self.rows})


@pytest.fixture
def ai_configured(monkeypatch):
    def set_(value: bool):
        monkeypatch.setattr(type(settings), "deepseek_configured", property(lambda self: value))
    set_(False)
    return set_


@pytest.fixture
def run(monkeypatch, ai_configured):
    monkeypatch.setenv("AI_BOX_SERVICE_URL", "http://ai-box.test")
    monkeypatch.setenv("AI_BOX_SERVICE_TOKEN", "box-secret")

    def go(upstream, body=None, headers=HEADERS, ai=None):
        ai = ai or FakeAI(json.loads(json.dumps(NARRATIVE)))
        transport = httpx.MockTransport(upstream)
        service = ReportService(ai, config=settings, channel_source=ChannelSource(transport=transport))
        service._meta_source = MetaSource(transport=transport)
        app.dependency_overrides[get_report_service] = lambda: service
        try:
            with TestClient(app) as client:
                request = {"report_type": "shipment", "source": "shipment_api", "prompt": "How did WhatsApp do?",
                           "filters": FILTERS, "data_sources": ["whatsapp"], **(body or {})}
                return client.post("/api/reports/generate", json=request, headers=headers)
        finally:
            app.dependency_overrides.clear()
    return go


def _error(response) -> dict:
    return response.json()["error"]


def test_whatsapp_report_uses_only_synced_values(run):
    upstream = AiBox(context(whatsapp=OK))
    response = run(upstream)
    assert response.status_code == 200, response.text
    report = ReportResponse.model_validate(response.json())
    assert report.report_type == "social_media"
    table = report.tables[0]
    assert table.columns == ["Metric", "Value", "Basis"]
    assert ["Messages sent", "1,840", "Total of 3 days with data"] in table.rows
    assert ["Billable messages - MARKETING", "400", "Total of 3 days with data"] in table.rows
    section = report.structured_report.sections[0]
    assert section.title == "WhatsApp Business: Acme Support WABA"
    assert any(b.type == "bullet_list" and "Template messages read: permission not granted to BigShip" in b.items
               for b in section.blocks)
    chart = report.structured_report.charts[0]
    assert chart.type == "line" and chart.labels == DAYS and chart.datasets[0].data == [600, 640, 600]
    assert "Counts are aggregates from Meta." in report.content.data_limitations
    assert report.validation_token   # sealed: exports accept it

    sent = upstream.requests[0]
    assert sent.url.path == "/api/meta/report-context"
    assert sent.headers["X-Shipment-Authorization"] == TOKEN
    assert sent.headers["X-Ai-Box-Service-Token"] == "box-secret"
    assert json.loads(sent.content) == {"data_sources": ["whatsapp"], "from_date": FROM_DAY.isoformat(),
                                        "to_date": TO_DAY.isoformat(), "timezone": "Asia/Kolkata"}


def test_unconnected_source_is_a_structured_connect_error(run):
    response = run(AiBox(context(whatsapp=OK, instagram=NOT_CONNECTED)),
                   body={"data_sources": ["whatsapp", "instagram"]})
    assert response.status_code == 409
    error = _error(response)
    assert error["code"] == "meta_not_connected"
    assert error["details"] == {"platforms": ["instagram"], "action": "connect"}


def test_expired_authorization_asks_for_reconnect(run):
    expired = {"status": "reconnect_required", "accounts": [whatsapp_account(reconnect_required=True,
                                                                             connection_status="expired")]}
    response = run(AiBox(context(whatsapp=expired)))
    assert response.status_code == 409
    assert _error(response)["code"] == "meta_reconnect_required"
    assert _error(response)["details"]["action"] == "reconnect"


def test_no_synced_data_is_reported_as_such_not_as_zero(run):
    empty = {"status": "no_data", "accounts": [whatsapp_account(
        metrics={}, last_successful_sync_at=None,
        coverage={"first_date": None, "last_date": None, "days_with_data": 0, "requested_days": 3, "complete": False},
        unavailable_metrics=[{"metric": "messages_sent", "reason": "not_synced", "detail": "Not synced yet."}])]}
    response = run(AiBox(context(whatsapp=empty)))
    assert response.status_code == 422
    error = _error(response)
    assert error["code"] == "meta_no_data"
    assert "first sync has not finished yet" in error["message"]


def test_stale_data_gets_a_visible_freshness_note(run):
    stale = {"status": "stale", "accounts": [whatsapp_account(data_stale=True)], "caveats": []}
    response = run(AiBox(context(whatsapp=stale)))
    assert response.status_code == 200, response.text
    first = response.json()["structured_report"]["sections"][0]["blocks"][0]["text"]
    assert "may be out of date" in first


def test_rejected_bigship_login_is_reported_as_auth_failure(run):
    response = run(AiBox(status=401, detail={"code": "META_SELLER_UNAUTHENTICATED"}))
    assert response.status_code == 401
    assert _error(response)["code"] == "upstream_auth_failed"


def test_meta_not_configured_on_ai_box_service(run):
    response = run(AiBox(status=503, detail={"code": "META_PLATFORM_NOT_CONFIGURED"}))
    assert response.status_code == 503
    assert _error(response)["code"] == "meta_not_configured"


def test_login_token_is_required(run):
    upstream = AiBox(context(whatsapp=OK))
    response = run(upstream, headers={"X-User-Id": "102795"})
    assert response.status_code == 401
    assert _error(response)["code"] == "shipment_auth_required"
    assert upstream.requests == []


def test_ai_observations_must_quote_supplied_numbers_only(run, ai_configured):
    ai_configured(True)
    ai = FakeAI({"findings": [
        {"title": "Steady volume", "detail": "1,840 messages were sent over 3 days."},
        {"title": "High delivery", "detail": "Delivery rate was 92% for the period."},   # invented: dropped
    ]})
    response = run(AiBox(context(whatsapp=OK)), ai=ai)
    assert response.status_code == 200, response.text
    findings = response.json()["structured_report"]["findings"]
    assert [f["title"] for f in findings] == ["Steady volume"]
    sent = json.dumps(ai.messages)
    assert "Acme Support WABA" not in sent and "1000000000000001" not in sent   # aliases only
    assert response.json()["generation"]["ai_calls"] == 1


def test_shipments_and_whatsapp_are_reported_side_by_side(run):
    upstream = AiBox(context(whatsapp=OK), rows=channel_orders(30))
    response = run(upstream, body={"source": "channel_api", "filters": CHANNEL_FILTERS,
                                   "data_sources": ["shipments", "whatsapp"], "prompt": "Order status overview"})
    assert response.status_code == 200, response.text
    report = ReportResponse.model_validate(response.json())
    assert report.report_type == "combined"
    ids = [s.id for s in report.structured_report.sections]
    assert any(i.startswith("shipments_") for i in ids) and "meta_whatsapp_1" in ids
    assert report.records_analyzed >= 30
    assert {r.url.path for r in upstream.requests} == {"/api/meta/report-context",
                                                       "/api/channel_wise_report/channels/13/orders"}


def test_social_only_question_leaves_shipments_out(run):
    upstream = AiBox(context(whatsapp=OK), rows=channel_orders(30))
    response = run(upstream, body={"source": "channel_api", "filters": CHANNEL_FILTERS,
                                   "data_sources": ["shipments", "whatsapp"],
                                   "prompt": "Summarise my WhatsApp performance and engagement trends"})
    assert response.status_code == 200, response.text
    report = ReportResponse.model_validate(response.json())
    assert report.report_type == "social_media"
    assert not any(s.id.startswith("shipments_") for s in report.structured_report.sections)
    assert any("Shipments are not included" in n for n in report.content.data_limitations)
    assert {r.url.path for r in upstream.requests} == {"/api/meta/report-context"}


@pytest.mark.parametrize("prompt, expected", [
    ("Summarise my Instagram performance: top posts, reach and engagement", True),
    ("Facebook page reach last week", True),
    ("इंस्टाग्राम पर मेरी पहुंच कैसी रही", True),
    ("Compare Instagram engagement with my order volume", False),
    ("WhatsApp messages vs RTO rate", False),
    ("Order status overview", False),
    ("", False),
])
def test_social_only_prompt(prompt, expected):
    from app.services.meta_reports import social_only_prompt
    assert social_only_prompt(prompt) is expected


def test_data_sources_validation_keeps_old_requests_unchanged():
    base = {"report_type": "shipment", "source": "shipment_api", "filters": FILTERS}
    assert ReportRequest.model_validate(base).meta_sources == []
    assert ReportRequest.model_validate({**base, "data_sources": ["shipments"]}).meta_sources == []
    with pytest.raises(ValidationError):
        ReportRequest.model_validate({**base, "data_sources": ["whatsapp", "whatsapp"]})
    with pytest.raises(ValidationError):
        ReportRequest.model_validate({"report_type": "shipment", "data": {"getOrderList": []},
                                      "data_sources": ["instagram"]})
    with pytest.raises(ValidationError):
        ReportRequest.model_validate({**base, "data_sources": ["tiktok"]})
