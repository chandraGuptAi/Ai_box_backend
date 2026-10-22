"""Backend-owned shipment fetch (source="shipment_api") against a mocked shipment API.

No live seller calls or credentials: every upstream response comes from httpx.MockTransport.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.report_routes import get_report_service
from app.config import settings
from app.main import app
from app.models.report_schema import ReportFilters, ReportResponse
from app.services.report_service import ReportService
from app.services.shipment_source import ShipmentSource, build_query
from tests.test_dynamic_reports import NARRATIVE, make_orders
from tests.test_reports import FakeAI, isolated_validation_audit  # noqa: F401  (fixture)

TOKEN = "Bearer seller-login-token-123456"
HEADERS = {"X-User-Id": "102795", "X-Shipment-Authorization": TOKEN}
TO_DAY = date.today() - timedelta(days=1)
FROM_DAY = TO_DAY - timedelta(days=29)
FILTERS = {"date_field": "manifest_datetime", "from_date": FROM_DAY.isoformat(), "to_date": TO_DAY.isoformat(),
           "status": "Delivered", "timezone": "Asia/Kolkata"}


def rows(n, **extra):
    orders = make_orders(n, statuses=("Delivered",))
    for i, order in enumerate(orders):
        order.update({"awb_number": f"AWB{i:06d}", **extra})
    return orders


class Upstream:
    """Serves ``data`` like /api/get-order-list; knobs reproduce each failure mode."""

    def __init__(self, data, *, cap=None, totals=None, status=None, body=None, script=None):
        self.data, self.cap, self.totals, self.status, self.body = data, cap, list(totals or []), status, body
        self.script = script          # (page, per_page, call) -> rows override
        self.requests: list[dict] = []
        self.headers: list[httpx.Headers] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        self.headers.append(request.headers)
        if callable(self.status):
            result = self.status(len(self.requests))
            if isinstance(result, httpx.Response):
                return result
        elif self.status:
            return httpx.Response(self.status, headers={"Retry-After": "30"}, json={"message": "secret upstream text"})
        if self.body is not None:
            return httpx.Response(200, content=self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode())
        page, size = payload["page"], payload["perPage"]
        size = min(size, self.cap) if self.cap else size
        chunk = self.data[(page - 1) * size: page * size]
        if self.script:
            chunk = self.script(page, size, len(self.requests), chunk)
        total = self.totals.pop(0) if self.totals else len(self.data)
        return httpx.Response(200, json={"success": True, "status_code": 200, "message": "ok",
                                         "data": {"getOrderList": chunk, "totalRecords": total, "all_orders": 900}})


@pytest.fixture
def ai():
    fake = FakeAI(json.loads(json.dumps(NARRATIVE)))
    return fake


@pytest.fixture
def run(ai):
    def go(upstream, body=None, headers=HEADERS, **config):
        cfg = replace(settings, **{"shipment_api_base_url": "https://shipments.test", **config})
        service = ReportService(ai, config=cfg, source=ShipmentSource(cfg, transport=httpx.MockTransport(upstream)))
        app.dependency_overrides[get_report_service] = lambda: service
        try:
            with TestClient(app) as client:
                request = {"report_type": "shipment", "source": "shipment_api",
                           "prompt": "Shipment status overview", "filters": FILTERS, **(body or {})}
                return client.post("/api/reports/generate", json=request, headers=headers)
        finally:
            app.dependency_overrides.clear()
    return go


def error(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()["error"]
    assert body["code"] == code
    assert body["request_id"] and body["request_id"] == response.headers["x-request-id"]
    assert "secret upstream text" not in response.text and "seller-login-token" not in response.text
    return body


# --- Two-stage fetch --------------------------------------------------------------------------------

def test_count_then_one_full_page(run, ai, coverage_audit):
    upstream = Upstream(rows(272))
    response = run(upstream)
    assert response.status_code == 200, response.text
    report = ReportResponse.model_validate(response.json())
    assert [(r["page"], r["perPage"]) for r in upstream.requests] == [(1, 1), (1, 272)]
    # Identical filters and credential on both requests; only page/perPage differ.
    strip = [{k: v for k, v in r.items() if k not in ("page", "perPage")} for r in upstream.requests]
    assert strip[0] == strip[1]
    assert strip[0]["type"] == 7 and strip[0]["date_filter_type"] == "manifest_datetime"
    assert strip[0]["only_order_stats"] == 0 and "total" not in strip[0]
    assert {h["authorization"] for h in upstream.headers} == {TOKEN}
    assert all("x-user-id" not in h for h in upstream.headers)  # AI Box identity is never forwarded

    assert "completeness" not in report.structured_report.scope  # audit data, not report content
    record = coverage_audit[-1]
    assert record["outcome"] == "generated" and record["report_id"] == report.report_id
    assert record["user_id"] == "102795" and record["request_id"] == response.headers["x-request-id"]
    completeness = record["completeness"]
    assert (completeness["expected_records"], completeness["fetched_records"], completeness["unique_records"],
            completeness["analyzed_records"]) == (272, 272, 272, 272)
    assert completeness["status"] == "complete" and completeness["source"] == "shipment_api"
    assert completeness["fetched_at"] and completeness["source_snapshot"] is None and completeness["data_as_of"] is None
    assert report.records_analyzed == 272


def test_missing_date_field_defaults_to_manifest_on_every_upstream_call(run):
    upstream = Upstream(rows(8))
    filters = {key: value for key, value in FILTERS.items() if key != "date_field"}
    response = run(upstream, body={"filters": filters})
    assert response.status_code == 200, response.text
    assert all(request["date_filter_type"] == "manifest_datetime" for request in upstream.requests)
    assert response.json()["structured_report"]["scope"]["selected_period"]["date_field"] == "manifest_datetime"


@pytest.mark.parametrize("configured", [None, "", "avio"])
def test_domestic_segment_is_set_in_code_independently_of_environment(monkeypatch, configured):
    from app.config import Settings
    if configured is None:
        monkeypatch.delenv("SHIPMENT_API_SEGMENT_TYPE", raising=False)
    else:
        monkeypatch.setenv("SHIPMENT_API_SEGMENT_TYPE", configured)
    assert Settings.__dataclass_fields__["shipment_api_segment_type"].default == "domestic"
    assert Settings.from_env().shipment_api_segment_type == "domestic"


def test_ist_dates_and_domestic_segment_are_identical_on_count_and_data_requests(run):
    upstream = Upstream(rows(8))
    response = run(upstream, shipment_api_segment_type="domestic")
    assert response.status_code == 200, response.text
    assert len(upstream.requests) == 2
    for request in upstream.requests:
        assert request["segment_type"] == "domestic"
        assert request["fromDate"] == FROM_DAY.isoformat()+" 00:00:00"
        assert request["toDate"] == (TO_DAY+timedelta(days=1)).isoformat()+" 00:00:00"


def test_total_records_is_the_only_total(run):
    upstream = Upstream(None, body={"success": True, "data": {"getOrderList": [], "total": 5, "all_orders": 5}})
    error(run(upstream), 502, "upstream_invalid_response")


def test_zero_matching_records(run, ai):
    body = error(run(Upstream([])), 422, "no_matching_records")
    assert body["details"]["expected_records"] == 0
    assert ai.calls == 0


def test_silently_capped_page_size_is_read_page_by_page(run, coverage_audit):
    upstream = Upstream(rows(272), cap=100)
    response = run(upstream)
    assert response.status_code == 200, response.text
    assert [(r["page"], r["perPage"]) for r in upstream.requests] == [(1, 1), (1, 272), (1, 100), (2, 100), (3, 100)]
    completeness = coverage_audit[-1]["completeness"]
    assert completeness["page_size_clamped"] is True and completeness["analyzed_records"] == 272


def test_large_total_uses_bounded_pages(run):
    upstream = Upstream(rows(250))
    response = run(upstream, shipment_api_max_page_size=100)
    assert response.status_code == 200, response.text
    assert [r["perPage"] for r in upstream.requests] == [1, 100, 100, 100]


def test_repeated_page_fails_after_bounded_retry(run, ai):
    data = rows(250)
    upstream = Upstream(data, script=lambda page, size, call, chunk: data[:size])
    body = error(run(upstream, shipment_api_max_page_size=100), 502, "upstream_pagination_inconsistent")
    assert body["retryable"] is True
    assert len(upstream.requests) == 6  # two whole attempts of count + page 1 + page 2
    assert ai.calls == 0


def test_empty_page_before_completion(run, ai):
    upstream = Upstream(rows(250), script=lambda page, size, call, chunk: [] if page == 2 else chunk)
    body = error(run(upstream, shipment_api_max_page_size=100), 502, "upstream_pagination_inconsistent")
    assert body["details"]["page"] == 2
    assert ai.calls == 0


def test_rows_shifting_between_pages_is_a_source_change(run):
    data = rows(250)
    shifted = lambda page, size, call, chunk: data[(page - 1) * size - 1: page * size - 1] if page == 2 else chunk
    error(run(Upstream(data, script=shifted), shipment_api_max_page_size=100), 409, "source_changed")


def test_total_changing_during_fetch(run, ai):
    body = error(run(Upstream(rows(10), totals=[10, 11, 10, 12])), 409, "source_changed")
    assert body["retryable"] is True and body["details"]["latest_total"] in (11, 12)
    assert ai.calls == 0


def test_exact_duplicates_in_the_source_are_removed_not_missing(run, coverage_audit):
    data = rows(5)
    data[4] = dict(data[3])
    response = run(Upstream(data))
    assert response.status_code == 200, response.text
    completeness = coverage_audit[-1]["completeness"]
    assert completeness["duplicate_records_removed"] == 1 and completeness["missing_records"] == 0
    assert completeness["analyzed_records"] == 4


def test_conflicting_versions_of_a_shipment(run, ai):
    data = rows(5)
    data[4] = {**data[3], "totalCharge": 999}
    body = error(run(Upstream(data)), 502, "conflicting_records")
    assert body["retryable"] is True and body["details"]["conflicting_duplicates"] == 1
    assert ai.calls == 0


# --- Dates, statuses and verification ---------------------------------------------------------------

def test_ist_boundaries_for_selected_calendar_days():
    filters = ReportFilters(date_field="manifest_datetime", from_date="2026-10-01", to_date="2026-10-12",
                            timezone="Asia/Kolkata")
    query = build_query(filters, settings, today=date(2026, 10, 21))
    assert (query.from_date, query.to_date) == ("2026-10-01 00:00:00", "2026-10-13 00:00:00")  # end exclusive
    utc = build_query(filters.model_copy(update={"timezone": "UTC"}), settings, today=date(2026, 10, 21))
    assert (utc.from_date, utc.to_date) == ("2026-10-01 05:30:00", "2026-10-13 05:30:00")
    assert query.status_type == 0 and query.status_label == "All Statuses"


def test_missing_manifest_datetime_is_not_replaced_by_order_date(run, coverage_audit):
    # Order dates are a year earlier: they must not be read as the manifest date.
    data = rows(6)
    for order in data:
        order["orderDate"] = order["createdAt"] = "2025-01-05"
    response = run(Upstream(data))
    assert response.status_code == 200, response.text
    report = response.json()["structured_report"]
    completeness = coverage_audit[-1]["completeness"]
    assert completeness["filters"]["applied_at_source"] is True
    assert completeness["filters"]["date"]["verification"] == "not_verifiable"
    assert completeness["outside_selected_period"] == 0
    assert completeness["scope_verification"] == "partially_verified"  # status verified, date not
    assert any("manifest datetime filter was applied by the shipment service but could not be checked" in n
               for n in coverage_audit[-1]["notes"])
    assert any("could not be checked" in n for n in report["limitations"])  # truthful scope coverage remains public
    assert "outside the API filter period" not in report["scope"]["statement"]


def test_manifest_datetime_is_verified_when_present(run, coverage_audit):
    inside = (FROM_DAY + timedelta(days=3)).isoformat() + "T06:00:00+05:30"
    response = run(Upstream(rows(4, manifest_datetime=inside)))
    assert response.status_code == 200, response.text
    completeness = coverage_audit[-1]["completeness"]
    assert completeness["filters"]["date"]["verification"] == "verified"
    assert completeness["scope_verification"] == "verified"


def test_manifest_datetime_outside_selection_is_a_source_mismatch(run, ai):
    data = rows(4, manifest_datetime=(FROM_DAY + timedelta(days=3)).isoformat() + "T06:00:00+05:30")
    data[0]["manifest_datetime"] = (FROM_DAY - timedelta(days=1)).isoformat() + "T12:00:00+05:30"
    body = error(run(Upstream(data)), 502, "scope_mismatch")
    assert body["details"]["outside_selected_period"] == 1 and body["retryable"] is False
    assert ai.calls == 0


def test_day_boundary_in_local_time(run):
    # 00:30 IST on the first day is inside; 00:30 IST the day after the last day is outside.
    data = rows(2)
    data[0]["manifest_datetime"] = (FROM_DAY - timedelta(days=1)).isoformat() + "T19:00:00Z"
    data[1]["manifest_datetime"] = TO_DAY.isoformat() + "T19:00:00Z"
    body = error(run(Upstream(data)), 502, "scope_mismatch")
    assert body["details"]["outside_selected_period"] == 1 and body["details"]["filters"]["date"]["checked_records"] == 2


def test_naive_timestamp_at_midnight_is_ambiguous_not_guessed(run, coverage_audit):
    data = rows(2, manifest_datetime=(FROM_DAY + timedelta(days=5)).isoformat() + " 12:00:00")
    data[1]["manifest_datetime"] = TO_DAY.isoformat() + " 22:00:00"  # local: inside; UTC: next day in IST
    response = run(Upstream(data))
    assert response.status_code == 200, response.text
    date_check = coverage_audit[-1]["completeness"]["filters"]["date"]
    assert date_check["ambiguous_records"] == 1 and date_check["verification"] == "partially_verified"


def test_prompt_status_conflict_asks_for_clarification(run, ai):
    body = error(run(Upstream(rows(5)), {"prompt": "Show cancelled shipments by courier"}), 422, "clarification_required")
    assert body["details"]["conflict"] == "status"
    assert ai.calls == 0


@pytest.mark.parametrize(("filters", "field"), [
    ({**FILTERS, "status": "Teleported"}, "status"),
    ({**FILTERS, "date_field": "order_date"}, "date_field"),
    ({**FILTERS, "to_date": (date.today() + timedelta(days=2)).isoformat()}, "to_date"),
    ({**FILTERS, "from_date": (TO_DAY - timedelta(days=400)).isoformat()}, "from_date"),
    ({**FILTERS, "timezone": "Mars/Base"}, "timezone"),
])
def test_unsupported_filters_are_rejected_before_any_call(run, filters, field):
    upstream = Upstream(rows(3))
    body = error(run(upstream, {"filters": filters}), 422, "unsupported_filter")
    assert body["details"]["field"] == field and upstream.requests == []


def test_request_contract_is_unambiguous(run):
    upstream = Upstream(rows(3))
    response = run(upstream, {"data": {"getOrderList": rows(3)}})
    assert response.status_code == 422 and response.json()["error"]["code"] == "request_validation_error"
    response = run(upstream, {"expected_records": 3})
    assert response.status_code == 422
    response = run(upstream, {"filters": {**FILTERS, "from_date": TO_DAY.isoformat(), "to_date": FROM_DAY.isoformat()}})
    assert response.status_code == 422
    assert upstream.requests == []


# --- Authentication -----------------------------------------------------------------------------------

def test_ai_box_login_and_shipment_credential_are_distinct(run):
    upstream = Upstream(rows(3))
    error(run(upstream, headers={"X-Shipment-Authorization": TOKEN}), 401, "ai_box_auth_required")
    error(run(upstream, headers={"X-User-Id": "102795"}), 401, "shipment_auth_required")
    assert upstream.requests == []


SANCTUM = "Bearer 4821|q7Zr9xKpL2mN8vB3cD6fG1hJ0kT5wY4uE7iO2aS9"  # opaque, not a JWT; "|" must be accepted


def test_opaque_token_reaches_every_upstream_call(run):
    # 503 on the first count call forces a retry; cap=4 forces the clamped page-by-page fallback.
    upstream = Upstream(rows(10), cap=4, status=lambda call: httpx.Response(503) if call == 1 else None)
    response = run(upstream, headers={"X-User-Id": "102795", "X-Shipment-Authorization": SANCTUM})
    assert response.status_code == 200, response.text
    assert [(r["page"], r["perPage"]) for r in upstream.requests] == [(1, 1), (1, 1), (1, 10), (1, 4), (2, 4), (3, 4)]
    assert [h["authorization"] for h in upstream.headers] == [SANCTUM] * 6


def test_bearer_scheme_is_case_insensitive_and_normalised(run):
    upstream = Upstream(rows(3))
    response = run(upstream, headers={"X-User-Id": "102795",
                                      "X-Shipment-Authorization": "  bearer   abc.def-123  "})
    assert response.status_code == 200, response.text
    assert {h["authorization"] for h in upstream.headers} == {"Bearer abc.def-123"}


@pytest.mark.parametrize("value", ["Token abc123", "Bearer", "Bearer ", "Bearer null", "Bearer undefined",
                                   "Bearer two words", "abc123", "Basic dXNlcjpwYXNz"])
def test_malformed_credential_fails_before_any_upstream_call(run, ai, value):
    upstream = Upstream(rows(3))
    body = error(run(upstream, headers={"X-User-Id": "102795", "X-Shipment-Authorization": value}),
                 401, "shipment_auth_invalid")
    assert body["category"] == "source" and body["retryable"] is False
    assert upstream.requests == [] and ai.calls == 0
    secret = value.split(" ", 1)[-1]
    if len(secret) > 6:
        assert secret not in json.dumps(body)


def test_concurrent_users_keep_their_own_credentials():
    import asyncio

    cfg = replace(settings, shipment_api_base_url="https://shipments.test")
    seen: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers["authorization"]
        await asyncio.sleep(0.01)  # interleave the two users' requests
        seen.append((auth, json.loads(request.content)["fromDate"]))
        owner = auth.rsplit("-", 1)[-1]
        data = [{"order_id": f"{owner}-{i}", "status": "Delivered"} for i in range(5)]
        payload = json.loads(request.content)
        chunk = data[(payload["page"] - 1) * payload["perPage"]: payload["page"] * payload["perPage"]]
        return httpx.Response(200, json={"success": True, "data": {"getOrderList": chunk, "totalRecords": 5}})

    source = ShipmentSource(cfg, transport=httpx.MockTransport(handler))
    query = build_query(ReportFilters(**FILTERS), cfg)

    async def both():
        return await asyncio.gather(source.fetch(query, "Bearer token-alice"), source.fetch(query, "Bearer token-bob"))

    alice, bob = asyncio.run(both())
    assert {r["order_id"].split("-")[0] for r in alice.rows} == {"alice"}
    assert {r["order_id"].split("-")[0] for r in bob.rows} == {"bob"}
    assert sorted(a for a, _ in seen) == ["Bearer token-alice"] * 2 + ["Bearer token-bob"] * 2
    assert "authorization" not in source._client.headers  # never a shared client default


def test_cors_preflight_accepts_the_shipment_header():
    with TestClient(app) as client:
        response = client.options("/api/reports/generate", headers={
            "Origin": "http://localhost:4200", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-user-id,x-user-name,x-shipment-authorization"})
    assert response.status_code == 200
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "x-shipment-authorization" in allowed and "x-user-id" in allowed
    assert response.headers["access-control-allow-origin"] == "http://localhost:4200"


def test_credentials_never_reach_logs_errors_prompts_or_reports(run, ai, caplog):
    caplog.set_level("DEBUG")
    ok = run(Upstream(rows(6)), headers={"X-User-Id": "102795", "X-Shipment-Authorization": SANCTUM})
    rejected = run(Upstream(rows(6), status=401), headers={"X-User-Id": "102795", "X-Shipment-Authorization": SANCTUM})
    assert ok.status_code == 200 and rejected.status_code == 401
    secret = SANCTUM.split(" ", 1)[1]
    for text in (caplog.text, ok.text, rejected.text, json.dumps(ai.messages)):
        assert secret not in text and "4821|" not in text


@pytest.mark.parametrize(("status", "http", "code", "retryable"), [
    (401, 401, "upstream_auth_failed", False),
    (403, 403, "upstream_forbidden", False),
    (429, 429, "upstream_rate_limited", True),
    (500, 502, "upstream_unavailable", True),
    (404, 502, "upstream_rejected", False),
])
def test_upstream_http_failures(run, ai, status, http, code, retryable):
    response = run(Upstream(rows(3), status=status))
    body = error(response, http, code)
    assert body["retryable"] is retryable
    if status == 429:
        assert response.headers["retry-after"] == "30"
    assert ai.calls == 0


def test_server_errors_are_retried_once(run):
    upstream = Upstream(rows(3), status=lambda call: httpx.Response(503) if call == 1 else None)
    assert run(upstream).status_code == 200
    assert len(upstream.requests) == 3  # failed count, retried count, full page


def test_timeout(run, ai):
    def slow(call):
        raise httpx.ReadTimeout("slow")
    body = error(run(Upstream(rows(3), status=slow)), 504, "upstream_timeout")
    assert body["retryable"] is True and ai.calls == 0


def test_network_failure(run):
    def down(call):
        raise httpx.ConnectError("refused")
    error(run(Upstream(rows(3), status=down)), 502, "upstream_unavailable")


@pytest.mark.parametrize("body", [
    b"<html>gateway</html>",
    {"success": False, "status_code": 500, "message": "secret upstream text"},
    {"success": True, "status_code": 200, "data": {"getOrderList": "x", "totalRecords": 3}},
    {"success": True, "data": {"getOrderList": [], "totalRecords": -1}},
    {"success": True, "data": {"getOrderList": [], "totalRecords": True}},
])
def test_malformed_upstream_responses(run, ai, body):
    response = run(Upstream(None, body=body))
    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] in ("upstream_invalid_response", "upstream_rejected")
    assert "secret upstream text" not in response.text and ai.calls == 0


def test_in_body_auth_failure_on_http_200(run):
    error(run(Upstream(None, body={"success": False, "status_code": 401, "message": "Unauthenticated"})),
          401, "upstream_auth_failed")


# --- Limits, configuration, compatibility --------------------------------------------------------------

def test_record_limit_stops_before_the_full_fetch(run, ai):
    upstream = Upstream(rows(30))
    body = error(run(upstream, shipment_api_max_records=20), 422, "source_too_large")
    assert body["details"] == {"expected_records": 30, "max_records": 20}
    assert len(upstream.requests) == 1 and ai.calls == 0


def test_response_size_limit(run):
    error(run(Upstream(rows(40)), shipment_api_max_response_bytes=2048), 422, "source_too_large")


def test_not_configured(run):
    error(run(Upstream(rows(3)), shipment_api_base_url=""), 503, "shipment_source_not_configured")
    error(run(Upstream(rows(3)), shipment_api_base_url="http://evil.example"), 503, "shipment_source_not_configured")


def test_report_and_exports_remain_compatible(run, ai):
    response = run(Upstream(rows(12)))
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["structured_report"]["charts"] or report["tables"]
    context = json.dumps(ai.messages)
    assert "Private Person" not in context and "9876543210" not in context and "p@example.com" not in context
    assert "Selected filters: manifest datetime" in context and "source matched 12 records" in context
    with TestClient(app) as client:
        for path, magic in (("/api/reports/export/pdf", b"%PDF"), ("/api/reports/export/docx", b"PK")):
            exported = client.post(path, json=report)
            assert exported.status_code == 200 and exported.content.startswith(magic)
            assert 'filename="Bigship_' in exported.headers["content-disposition"]


def test_stopped_fetch_is_audited_without_the_credential(run, coverage_audit):
    response = run(Upstream(rows(3), status=401), headers={"X-User-Id": "102795", "X-Shipment-Authorization": SANCTUM})
    assert response.status_code == 401
    record = coverage_audit[-1]
    assert record["outcome"] == "upstream_auth_failed" and record["report_id"] is None
    assert record["request_id"] == response.headers["x-request-id"] and record["user_id"] == "102795"
    assert SANCTUM.split(" ", 1)[1] not in json.dumps(record)


def test_coverage_audit_file_is_one_json_line_per_report(tmp_path, monkeypatch):
    import logging
    from app.services import report_service
    from tests.conftest import real_write_coverage_audit

    monkeypatch.setattr(report_service, "BASE_DIR", tmp_path)
    audit = logging.getLogger("ai_box.coverage_audit")
    saved = list(audit.handlers)
    for handler in saved:
        audit.removeHandler(handler)
    try:
        real_write_coverage_audit({"event": "report_coverage", "report_id": "r1", "outcome": "generated",
                                   "completeness": {"expected_records": 251, "analyzed_records": 251}})
        real_write_coverage_audit({"event": "report_coverage", "report_id": None, "outcome": "incomplete_source"})
    finally:
        for handler in list(audit.handlers):
            audit.removeHandler(handler)
            handler.close()
        for handler in saved:
            audit.addHandler(handler)
    lines = (tmp_path / "output" / "report_coverage_audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["outcome"] for line in lines] == ["generated", "incomplete_source"]
    assert json.loads(lines[0])["completeness"]["analyzed_records"] == 251
