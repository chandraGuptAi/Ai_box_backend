"""Backend-owned channel-order fetch (source="channel_api") from a mocked Ai_Box_Service."""
from __future__ import annotations

import json
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.report_routes import get_report_service
from app.config import settings
from app.main import app
from app.models.report_schema import ReportResponse
from app.services.channel_source import ChannelSource
from app.services.report_service import ReportService
from tests.test_dynamic_reports import NARRATIVE
from tests.test_reports import FakeAI, isolated_validation_audit  # noqa: F401  (fixture)

TOKEN = "Bearer seller-login-token-123456"
HEADERS = {"X-User-Id": "102795", "X-Shipment-Authorization": TOKEN}
TO_DAY = date.today() - timedelta(days=1)
FROM_DAY = TO_DAY - timedelta(days=6)
FILTERS = {"from_date": FROM_DAY.isoformat(), "to_date": TO_DAY.isoformat(), "timezone": "Asia/Kolkata",
           "channel_id": 13}


def channel_orders(n: int) -> list[dict]:
    """Rows as Ai_Box_Service returns them (no customer name/phone/email/address)."""
    statuses = ("Unshipped", "Shipped", "Cancelled")
    return [{"channel_order_id": 500 + i, "order_number": f"#{1000 + i}",
             "order_date": f"{(FROM_DAY + timedelta(days=i % 7)).isoformat()}T06:00:00Z",
             "order_status": statuses[i % 3], "payment_status": "Paid", "fulfillment_status": "Unfulfilled",
             "payment_mode": "COD" if i % 2 else "Prepaid", "total_amount": 100.0 + i, "collectable_amount": 0.0,
             "items_count": 1, "delivery_status": None, "delivery_method": None, "tracking_number": None,
             "city": "Delhi", "state": "Delhi", "pincode": "110001", "country": "India"} for i in range(n)]


class AiBoxService:
    def __init__(self, rows=None, status=200):
        self.rows, self.status = rows or [], status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "upstream says no"})
        return httpx.Response(200, json={"channel_id": 13, "status": "all", "total_records": len(self.rows),
                                         "summary": {}, "orders": self.rows})


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setenv("AI_BOX_SERVICE_URL", "http://ai-box.test")
    monkeypatch.setenv("AI_BOX_SERVICE_TOKEN", "box-secret")

    def go(upstream, body=None, headers=HEADERS):
        ai = FakeAI(json.loads(json.dumps(NARRATIVE)))
        service = ReportService(ai, config=settings, channel_source=ChannelSource(transport=httpx.MockTransport(upstream)))
        app.dependency_overrides[get_report_service] = lambda: service
        try:
            with TestClient(app) as client:
                request = {"report_type": "shipment", "source": "channel_api",
                           "prompt": "Order status overview", "filters": FILTERS, **(body or {})}
                return client.post("/api/reports/generate", json=request, headers=headers)
        finally:
            app.dependency_overrides.clear()
    return go


def test_channel_orders_are_fetched_internally_and_analysed(run):
    upstream = AiBoxService(channel_orders(30))
    response = run(upstream)
    assert response.status_code == 200, response.text
    report = ReportResponse.model_validate(response.json())
    assert report.records_analyzed == 30

    request = upstream.requests[0]
    assert request.url.path == "/api/channel_wise_report/channels/13/orders"
    assert dict(request.url.params) == {"from_date": FROM_DAY.isoformat(), "to_date": TO_DAY.isoformat(), "status": "all"}
    assert request.headers["x-shipment-authorization"] == TOKEN
    assert request.headers["x-ai-box-service-token"] == "box-secret"
    assert "x-user-id" not in request.headers


def test_channel_api_requires_channel_id_and_rejects_data(run):
    upstream = AiBoxService()
    no_channel = run(upstream, body={"filters": {k: v for k, v in FILTERS.items() if k != "channel_id"}})
    assert no_channel.status_code == 422
    with_data = run(upstream, body={"data": {"getOrderList": []}})
    assert with_data.status_code == 422
    assert upstream.requests == []


def test_channel_id_is_rejected_for_shipment_api(run):
    response = run(AiBoxService(), body={"source": "shipment_api"})
    assert response.status_code == 422


@pytest.mark.parametrize("status, http_status, code", [
    (401, 401, "upstream_auth_failed"),
    (413, 422, "source_too_large"),
    (502, 502, "upstream_rejected"),
])
def test_ai_box_service_errors_are_mapped(run, status, http_status, code):
    response = run(AiBoxService(status=status))
    assert response.status_code == http_status, response.text
    assert response.json()["error"]["code"] == code
    assert "seller-login-token" not in response.text


def test_missing_credential_is_rejected_before_fetch(run):
    upstream = AiBoxService(channel_orders(3))
    response = run(upstream, headers={"X-User-Id": "102795"})
    assert response.status_code == 401
    assert upstream.requests == []


def test_channel_report_uses_order_labels_currency_scope_and_unique_kpis(monkeypatch):
    import asyncio
    from dataclasses import replace
    from io import BytesIO
    from pypdf import PdfReader
    from app.models.report_schema import ReportRequest
    from app.generators.pdf_generator import generate_pdf
    from tests.test_ordered_reports import PlannedAI, section

    monkeypatch.setenv('AI_BOX_SERVICE_URL', 'http://ai-box.test')
    rows = channel_orders(3)
    for row, status in zip(rows, ['Processing', 'Processing', 'Shipped']):
        row.update(order_status=status, total_amount=1200)
    upstream = AiBoxService(rows)
    ai = PlannedAI([
        section('total_orders', metrics=('orders', 'order_value')),
        section('orders_by_shipment_status', dimensions=('shipment_status',)),
    ])
    service = ReportService(ai, config=replace(settings, report_ai_planner=True),
        channel_source=ChannelSource(transport=httpx.MockTransport(upstream)))
    request = ReportRequest(report_type='shipment', source='channel_api', prompt='Order totals and status breakdown',
                            filters=FILTERS)
    report = asyncio.run(service.generate_report(request, shipment_credential=TOKEN, user_id='synthetic-user'))
    structured = report.structured_report
    assert structured.subtitle == 'Channel Orders Analysis Report'
    assert structured.scope['source_label'] == 'Channel orders (channel 13)'
    assert len(structured.metrics) == 2
    assert structured.metrics[1].value == '\u20b93,600.00'
    assert report.tables[0].values == [[3, 3600.0]]
    assert report.tables[0].rows == [['3', '\u20b93,600.00']]
    assert report.tables[1].columns[0] == 'Order Status'
    assert report.tables[1].values == [['Processing', 2], ['Shipped', 1]]
    rendered_text = '\n'.join(page.extract_text() for page in PdfReader(BytesIO(generate_pdf(report))).pages)
    assert 'Channel orders (channel 13)' in rendered_text
    assert 'shipments' not in rendered_text.lower()
    assert 'shipment' not in rendered_text.lower()
    assert 'Shipped' in rendered_text
    assert '\u20b93,600.00' in rendered_text
    assert 'Unshipped, Processing, Shipped, Cancelled, Fulfilled' in rendered_text
    planner_context = json.loads(ai.calls[0][-1]['content'])
    assert planner_context['normalized_category_values']['shipment_status'] == [
        'Unshipped', 'Processing', 'Shipped', 'Cancelled', 'Fulfilled']


def test_grounded_order_value_text_includes_currency_without_changing_counts():
    from app.analysis.section_reports import execute_registered_section, grounded_text
    from app.analysis.plan import ExecutionSection
    from app.charts.fields import get_adapter
    dataset = execute_registered_section(ExecutionSection(id='overview', title='Overview',
        requirement_ids=('summary',), metrics=('orders', 'order_value')),
        get_adapter('shipment').rows({'orders': [{'order_id': str(i), 'total_amount': 1200} for i in range(3)]}))
    for amount in ['3,600.00', '\u20b93,600.00']:
        raw = {'sections': [{'id': 'overview', 'claims': [{'text': f'3 orders have a total value of {amount}.',
                'metric_ids': ['overview.total.orders', 'overview.total.order_value']}]}]}
        text, repaired = grounded_text(raw, [dataset])
        assert not repaired
        assert text['overview'] == ['3 orders have a total value of \u20b93,600.00.']
