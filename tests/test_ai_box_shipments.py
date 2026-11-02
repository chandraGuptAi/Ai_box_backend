"""source=shipment_api through Ai_Box_Service: clean rows, lane/zone/pickup fields, errors, privacy note."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from app.analysis.accuracy import verify_revision
from app.config import settings
from app.errors import SourceTooLargeError
from app.models.report_schema import ReportRequest
from app.services.ai_box_shipments import AiBoxShipmentSource, default_shipment_source
from app.services.report_service import ReportService
from app.services.shipment_source import ShipmentSource
from tests.test_ordered_reports import PlannedAI, section

TOKEN = "Bearer seller-login-token-123456"
FILTERS = {"date_field": "manifest_datetime", "from_date": "2026-10-01", "to_date": "2026-10-21", "status": "All Statuses"}
REMOVED = [{"field": "consignee.companyName", "reason": "person or business name", "count": 6},
           {"field": "consignee.companyMobile", "reason": "contact detail", "count": 6},
           {"field": "invoiceFileURL", "reason": "file link", "count": 6}]


def clean_rows():
    """Rows as Ai_Box_Service returns them: no names, contacts, addresses or links; lane already split."""
    lanes = ["A-A", "A-A", "N1-E2", "N1-E2", "N1-E2", "West 1-South 2"]
    rows = []
    for i, lane in enumerate(lanes):
        origin, destination = [part.strip() for part in lane.split("-")]
        rows.append({"orderId": f"ORD-{i}", "awb_number": f"AWB-{i}", "status": "Delivered" if i % 2 else "Cancelled",
                     "courierName": "Delhivery 0.5 Special", "payment_mode": "Prepaid", "lane": lane,
                     "origin_zone": origin, "destination_zone": destination,
                     "consignor": {"city": "KOTA", "state": "RAJASTHAN", "pin": "325219" if i < 3 else "110037"},
                     "consignee": {"city": "DELHI", "state": "DELHI", "pin": "110011"},
                     "orderDate": "2026-10-02", "manifest_datetime": "2026-10-12T10:00:00+05:30",
                     "totalCharge": 70.5, "invoiceTotalAmount": "1500.00"})
    return rows


class AiBox:
    def __init__(self, rows=None, error=None):
        self.rows, self.error, self.requests = rows or [], error, []

    def __call__(self, request):
        self.requests.append(request)
        if self.error:
            return httpx.Response(self.error[0], json={"detail": self.error[1]})
        return httpx.Response(200, json={"rows": self.rows, "total": len(self.rows), "account_summary": {},
                                         "fetched_at": "2026-10-31T07:00:00+00:00", "requests": 2, "page_size": 6,
                                         "pages": 1, "attempts": 1, "page_size_clamped": False, "cached": False,
                                         "removed_fields": REMOVED})


@pytest.fixture
def ai_box(monkeypatch):
    monkeypatch.setenv("AI_BOX_SERVICE_URL", "http://ai-box.test")
    monkeypatch.setenv("AI_BOX_SERVICE_TOKEN", "box-secret")


def test_lane_pickup_pincode_and_zone_report_from_ai_box_service(ai_box):
    upstream = AiBox(clean_rows())
    ai = PlannedAI([section("by_lane", ("lane",)), section("by_pickup_pincode", ("origin_pincode",)),
                    section("by_destination_zone", ("zone",)), section("by_origin_zone", ("origin_zone",))])
    cfg = replace(settings, report_ai_planner=True)
    service = ReportService(ai, config=cfg, source=AiBoxShipmentSource(cfg, transport=httpx.MockTransport(upstream)))
    request = ReportRequest.model_validate({"report_type": "shipment", "source": "shipment_api",
                                            "prompt": "lane wise and pickup pincode wise shipments", "filters": FILTERS})
    report = asyncio.run(service.generate_report(request, shipment_credential=TOKEN, user_id="synthetic-account"))

    sent = upstream.requests[0]
    assert sent.url.path == "/api/shipments/fetch"
    assert sent.headers["x-shipment-authorization"] == TOKEN and sent.headers["x-ai-box-service-token"] == "box-secret"
    assert json.loads(sent.content)["status_type"] == 0
    assert report.records_analyzed == 6
    tables = {t.title: dict((row[0], row[1]) for row in t.values) for t in report.tables}
    assert tables["By Lane"] == {"N1-E2": 3, "A-A": 2, "West 1-South 2": 1}
    assert tables["By Pickup Pincode"] == {"325219": 3, "110037": 3}
    assert tables["By Destination Zone"] == {"E2": 3, "A": 2, "South 2": 1}
    assert tables["By Origin Zone"] == {"N1": 3, "A": 2, "West 1": 1}
    note = report.content.data_limitations[-1]
    assert note.startswith("Privacy:") and "consignee company name" in note and "contact detail" in note
    assert report.generation["privacy"]["source_fields"] == sorted(REMOVED, key=lambda f: (f["field"], f["reason"]))
    assert TOKEN not in json.dumps(ai.calls)
    verify_revision(report)


def test_service_error_codes_become_backend_errors(ai_box):
    upstream = AiBox(error=(422, {"code": "source_too_large", "message": "Too many shipments.",
                                  "details": {"expected_records": 9000}, "retryable": False}))
    source = AiBoxShipmentSource(settings, transport=httpx.MockTransport(upstream))
    from app.services.shipment_source import build_query
    from app.models.report_schema import ReportFilters
    query = build_query(ReportFilters.model_validate(FILTERS), settings)
    with pytest.raises(SourceTooLargeError) as error:
        asyncio.run(source.fetch(query, TOKEN))
    assert error.value.details == {"expected_records": 9000}


def test_ai_box_service_is_the_default_source_when_configured(monkeypatch):
    monkeypatch.setenv("SHIPMENT_SOURCE", "ai_box_service")
    monkeypatch.setenv("AI_BOX_SERVICE_URL", "http://ai-box.test")
    assert isinstance(default_shipment_source(settings), AiBoxShipmentSource)
    monkeypatch.setenv("AI_BOX_SERVICE_URL", "")
    assert isinstance(default_shipment_source(settings), ShipmentSource)


def test_pickup_and_destination_pincodes_are_told_apart():
    from app.analysis.interpreter import pincode_filters
    filters = {(f.field, f.op): f.value for f in pincode_filters("pickup pincode 110037 aur pincode 110011 ka report")}
    assert filters == {("origin_pincode", "in"): ("110037",), ("destination_pincode", "in"): ("110011",)}


@pytest.mark.parametrize("text, field", [("lane wise cancelled orders", "lane"),
                                         ("pickup pincode wise shipments", "origin_pincode"),
                                         ("pincode wise shipments", "destination_pincode"),
                                         ("origin zone wise orders", "origin_zone"),
                                         ("zone wise orders", "zone")])
def test_grouping_words_pick_the_right_field(text, field):
    from app.analysis.interpreter import _dimension_mentions
    from app.charts.fields import get_adapter
    assert _dimension_mentions(text, get_adapter("shipment"))[0][0] == field


def test_raw_rows_sent_in_the_request_get_lane_zones_locally():
    from app.charts.fields import get_adapter
    row = get_adapter("shipment").rows({"orders": [{"orderId": "1", "status": "Delivered", "lane": "N1-E2",
                                                    "consignor": {"pin": "110037", "city": "DELHI"}}]})[0]
    assert (row["lane"], row["origin_zone"], row["zone"], row["origin_pincode"], row["origin_city"]) == \
        ("N1-E2", "N1", "E2", "110037", "DELHI")
