"""Report period: prompt date extraction, the six-month limit, precedence over UI defaults, and its use end to end.

Unit tests fix "today"; integration tests go through POST /api/reports/generate with a mocked shipment API
(no live seller calls) and, where the privacy boundary matters, the real local guard with a fake AI provider.
"""
from __future__ import annotations

import asyncio
import copy
import io
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.report_routes import get_report_service
from app.config import settings
from app.errors import (ClarificationRequiredError, InvalidDateRangeError, ReportDateRangeExceededError,
                        UnsupportedFilterError)
from app.main import app
from app.models.report_schema import ReportFilters, ReportRequest
from app.report_period import (DateRangeResolver, ReportDateValidator, last_months_period, months_before,
                               resolve_report_period)
from app.security import privacy
from app.services.deepseek_service import DeepSeekService
from app.services.report_service import ReportService
from app.services.shipment_source import ShipmentSource
from tests.test_ordered_reports import PlannedAI, section
from tests.test_shipment_source import HEADERS, TOKEN, Upstream, ai, error, rows, run  # noqa: F401  (fixtures)

TODAY = date(2026, 10, 27)
IST = ZoneInfo("Asia/Kolkata")


def resolve(prompt, today=TODAY):
    period = DateRangeResolver().resolve(prompt, today)
    return None if period is None else (period.start, period.end)


# -- prompt extraction ---------------------------------------------------------------------------

@pytest.mark.parametrize("prompt, expected", [
    ("Show last 7 days shipments", (date(2026, 10, 21), TODAY)),
    ("Compare RTO and delivered for 30 days", (date(2026, 9, 28), TODAY)),
    ("Create insight report, comparison between RTO Transit and delivered shipment for 30 days.", (date(2026, 9, 28), TODAY)),
    ("Analyze shipments for 90 days", (date(2026, 7, 30), TODAY)),
    ("Generate report for 3 months", (date(2026, 7, 28), TODAY)),
    ("Show last month's performance", (date(2026, 9, 1), date(2026, 9, 30))),
    ("Analyze shipments from 1 Aug to 30 Sep 2026", (date(2026, 8, 1), date(2026, 9, 30))),
    ("Generate report for last 6 months", (date(2026, 4, 28), TODAY)),
    ("last 2 weeks of COD orders", (date(2026, 10, 14), TODAY)),
    ("report between 1 April 2026 and 30 September 2026", (date(2026, 4, 1), date(2026, 9, 30))),
    ("orders on 5 Oct 2026", (date(2026, 10, 5), date(2026, 10, 5))),
    ("COD vs Prepaid in September 2026", (date(2026, 9, 1), date(2026, 9, 30))),
    ("report from 2026-08-01 to 2026-08-31", (date(2026, 8, 1), date(2026, 8, 31))),
])
def test_english_periods(prompt, expected):
    assert resolve(prompt) == expected


@pytest.mark.parametrize("prompt, expected", [
    ("पिछले 30 दिनों की रिपोर्ट बनाओ", (date(2026, 9, 28), TODAY)),
    ("पिछले तीन महीने की डिलीवरी रिपोर्ट", (date(2026, 7, 28), TODAY)),
    ("पिछले महीने की रिपोर्ट", (date(2026, 9, 1), date(2026, 9, 30))),
    ("1 अगस्त से 30 सितंबर 2026 तक की रिपोर्ट", (date(2026, 8, 1), date(2026, 9, 30))),
    ("Pichle 2 mahine ki report banao", (date(2026, 8, 28), TODAY)),
    ("pichle 30 din ka RTO data", (date(2026, 9, 28), TODAY)),
    ("30 din ki report", (date(2026, 9, 28), TODAY)),
    ("pichle mahine ki report", (date(2026, 9, 1), date(2026, 9, 30))),
    ("पिछले ३० दिनों की रिपोर्ट", (date(2026, 9, 28), TODAY)),          # Devanagari digits
])
def test_hindi_and_hinglish_periods(prompt, expected):
    assert resolve(prompt) == expected


@pytest.mark.parametrize("prompt", ["Analyze courier performance", "Show shipments delivered within 3 days",
                                    "Orders older than 30 days by courier", "COD and Prepaid split"])
def test_prompts_without_a_reporting_period(prompt):
    assert resolve(prompt) is None


def test_multi_period_requests_ask_for_clarification():
    with pytest.raises(ClarificationRequiredError) as raised:
        resolve("compare this month with last month")
    assert raised.value.details["reason"] == "multiple_periods"
    assert len(raised.value.details["periods"]) == 2


@pytest.mark.parametrize("prompt, reason", [("from 30 Sep 2026 to 1 Aug 2026", "reversed_dates"),
                                            ("from 31 Feb 2026 to 10 Mar 2026", "invalid_date")])
def test_reversed_and_impossible_prompt_dates_are_rejected(prompt, reason):
    with pytest.raises(InvalidDateRangeError) as raised:
        resolve(prompt)
    assert raised.value.details["reason"] == reason


# -- six calendar months ---------------------------------------------------------------------

@pytest.mark.parametrize("prompt", ["last 30 days", "last 90 days", "last 3 months", "last 6 months",
                                    "from 1 Apr 2026 to 30 Sep 2026"])
def test_allowed_ranges(prompt):
    ReportDateValidator().validate(*resolve(prompt), source="prompt")


@pytest.mark.parametrize("prompt", ["last 7 months", "last 8 months", "last year", "last 365 days",
                                    "from 31 Mar 2026 to 30 Sep 2026", "from 1 Apr 2026 to 1 Oct 2026"])
def test_rejected_ranges(prompt):
    with pytest.raises(ReportDateRangeExceededError) as raised:
        ReportDateValidator().validate(*resolve(prompt), source="prompt")
    assert raised.value.details["max_months"] == 6 and raised.value.details["suggested_range"] == "last_6_months"


def test_exactly_six_months_is_inclusive_on_both_days():
    validator = ReportDateValidator()
    validator.validate(date(2026, 4, 1), date(2026, 9, 30), source="t")
    for start, end in ((date(2026, 3, 31), date(2026, 9, 30)), (date(2026, 4, 1), date(2026, 10, 1))):
        with pytest.raises(ReportDateRangeExceededError):
            validator.validate(start, end, source="t")


@pytest.mark.parametrize("end, start", [
    (date(2027, 2, 28), date(2026, 9, 1)),     # month end, non-leap year
    (date(2028, 2, 29), date(2027, 9, 1)),     # leap day
    (date(2028, 8, 30), date(2028, 3, 1)),     # 31 Aug - 6 months has no 31 Feb: never longer than six months
    (date(2026, 10, 27), date(2026, 4, 28)),
])
def test_last_six_months_at_month_end_and_leap_year(end, start):
    assert last_months_period(6, end) == (start, end)
    ReportDateValidator().validate(start, end, source="t")
    with pytest.raises(ReportDateRangeExceededError):
        ReportDateValidator().validate(start - timedelta(days=1), end, source="t")


def test_months_before_moves_missing_days_forward():
    assert months_before(date(2026, 8, 31), 6) == date(2026, 3, 1)
    assert months_before(date(2028, 8, 31), 6) == date(2028, 3, 2) - timedelta(days=1)  # 29 Feb 2028 exists
    assert months_before(date(2026, 3, 15), 6) == date(2025, 9, 15)


def test_old_periods_are_valid_when_short():
    ReportDateValidator().validate(*resolve("from 1 Jan 2020 to 31 Mar 2020"), source="prompt")


def test_reversed_structured_dates_are_rejected():
    with pytest.raises(ValueError):
        ReportFilters(from_date=date(2026, 10, 1), to_date=date(2026, 9, 1))
    with pytest.raises(InvalidDateRangeError):
        ReportDateValidator().validate(date(2026, 10, 1), date(2026, 9, 1), source="selected_filters")


# -- precedence ------------------------------------------------------------------------------

def api_request(prompt, filters=None, previous=None):
    return ReportRequest.model_validate({"report_type": "shipment", "source": "shipment_api", "prompt": prompt,
        "filters": filters, **({"previous_report": previous} if previous else {})})


def effective(request, today=TODAY):
    new, period = resolve_report_period(request, default_date_field="manifest_datetime", today=today)
    return new.filters, period


PRESET_7 = {"date_field": "manifest_datetime", "range_days": 7}
CUSTOM_7 = {"date_field": "manifest_datetime", "from_date": "2026-10-21", "to_date": "2026-10-27"}


def test_ui_default_preset_does_not_hide_a_prompt_period():
    filters, period = effective(api_request("Compare RTO and delivered for 30 days", PRESET_7),
                                today=date.today())
    assert (filters.from_date, filters.to_date) == (date.today() - timedelta(days=29), date.today())
    assert filters.range_days is None and period.source == "prompt" and period.label == "Last 30 days"
    assert period.selected["kind"] == "preset"


def test_date_source_default_lets_the_prompt_win_over_explicit_dates():
    filters, period = effective(api_request("for 30 days", {**CUSTOM_7, "date_source": "default"}))
    assert (filters.from_date, period.source) == (date(2026, 9, 28), "prompt")


@pytest.mark.parametrize("extra", [{}, {"date_source": "user"}])
def test_seller_selected_dates_win_and_a_conflict_is_never_silent(extra):
    with pytest.raises(ClarificationRequiredError) as raised:
        effective(api_request("Compare RTO and delivered for 30 days", {**CUSTOM_7, **extra}))
    assert raised.value.details["reason"] == "date_range_conflict"
    filters, period = effective(api_request("show yesterday's deliveries", {**CUSTOM_7, **extra}))
    assert (filters.from_date, filters.to_date, period.source) == (date(2026, 10, 21), TODAY, "selected_filters")
    assert period.prompt_period["from_date"] == "2026-10-26"      # analysed as a sub-period, as before


def test_without_a_prompt_period_the_structured_range_is_used():
    filters, period = effective(api_request("Courier performance", CUSTOM_7))
    assert (filters.from_date, period.source, period.label) == (date(2026, 10, 21), "selected_filters",
                                                                 "21 Oct 2026 to 27 Oct 2026")


def test_structured_ranges_over_six_months_are_rejected_unless_replaced():
    with pytest.raises(ReportDateRangeExceededError):
        effective(api_request("Courier performance", {"date_field": "manifest_datetime", "range_days": 365}))
    filters, _ = effective(api_request("last 30 days", {"date_field": "manifest_datetime", "range_days": 365}))
    assert filters.from_date == date(2026, 9, 28)


def previous_report(period):
    return {"report_id": "r0", "structured_report": {"scope": {"date_range": period}}}


def test_follow_up_keeps_the_prompt_period_of_the_report_it_continues():
    previous = previous_report({"source": "prompt", "label": "Last 30 days", "start_date": "2026-09-28",
                                "end_date": "2026-10-27"})
    filters, period = effective(api_request("add a courier chart", PRESET_7, previous))
    assert (filters.from_date, filters.to_date, period.source) == (date(2026, 9, 28), TODAY, "previous_report")


@pytest.mark.parametrize("prompt, previous", [
    ("now show the last 8 months", None),
    ("add a courier chart", {"source": "prompt", "label": "Last year", "start_date": "2025-01-01", "end_date": "2025-12-31"}),
])
def test_follow_ups_cannot_bypass_the_maximum_range(prompt, previous):
    with pytest.raises(ReportDateRangeExceededError):
        effective(api_request(prompt, PRESET_7, previous_report(previous) if previous else
                              previous_report({"source": "prompt", "start_date": "2026-09-28", "end_date": "2026-10-27"})))


def test_resolution_uses_the_seller_timezone():
    for zone in ("Pacific/Kiritimati", "Pacific/Pago_Pago", "Asia/Kolkata"):    # UTC+14 and UTC-11: different days
        filters, _ = resolve_report_period(api_request("last 30 days", {**PRESET_7, "timezone": zone}),
                                           default_date_field="manifest_datetime")
        assert filters.filters.to_date == datetime.now(ZoneInfo(zone)).date()
    with pytest.raises(UnsupportedFilterError):
        resolve_report_period(api_request("last 30 days", {**CUSTOM_7, "timezone": "Mars/Base"}),
                              default_date_field="manifest_datetime")


def test_payload_reports_only_check_limits():
    request = ReportRequest.model_validate({"report_type": "shipment", "prompt": "for 30 days", "data": {"orders": []},
                                            "filters": {"from_date": "2026-10-21", "to_date": "2026-10-27"}})
    new, period = resolve_report_period(request, default_date_field="manifest_datetime", today=TODAY)
    assert new is request and period.source == "selected_filters"


# -- end to end: source query, metadata, audit, exports --------------------------------------

def manifest_rows(n, **extra):
    """Rows manifested over the last 25 days (seller-local), inside any period of 30 days or more."""
    today = datetime.now(IST).date()
    return [{**row, "manifest_datetime": f"{today - timedelta(days=i % 25)}T10:00:00+05:30"}
            for i, row in enumerate(rows(n, **extra))]


def test_thirty_day_prompt_with_the_ui_default_fetches_thirty_days(run, ai, report_activity_audit):
    upstream = Upstream(manifest_rows(12))
    response = run(upstream, {"prompt": "Create insight report, comparison between RTO Transit and delivered "
                                        "shipment for 30 days.",
                              "filters": {**PRESET_7, "status": "All Statuses"}})
    assert response.status_code == 200, response.text
    today = datetime.now(IST).date()
    first = today - timedelta(days=29)
    assert {(r["fromDate"], r["toDate"]) for r in upstream.requests} == {
        (f"{first} 00:00:00", f"{today + timedelta(days=1)} 00:00:00")}
    assert all(h["authorization"] == TOKEN for h in upstream.headers)       # the seller's own delegated session
    scope = response.json()["structured_report"]["scope"]
    assert scope["date_range"] == {"source": "prompt", "label": "Last 30 days", "start_date": first.isoformat(),
        "end_date": today.isoformat(), "timezone": "Asia/Kolkata", "date_field": "manifest_datetime", "max_months": 6,
        "validation": "within_limit", "selected_filters": {"from_date": (today - timedelta(days=6)).isoformat(),
                                                           "to_date": today.isoformat(), "kind": "preset"}}
    assert scope["selected_period"]["from_date"] == first.isoformat()
    assert scope["requested_period"]["basis"] == "prompt"
    [record] = report_activity_audit
    assert (record["date_range_source"], record["resolved_start_date"], record["date_validation_result"],
            record["processing_stage"]) == ("prompt", first.isoformat(), "within_limit", "completed")
    assert record["seller_ref"].startswith("s_") and record["seller_id"] == record["user_id"] == "102795"
    assert record["filters"]["from_date"] == first.isoformat()          # the effective period, not the UI default
    assert TOKEN not in json.dumps(record)


def test_over_limit_prompt_stops_before_any_ai_call_or_shipment_query(run, ai, report_activity_audit):
    upstream = Upstream(manifest_rows(3))
    body = error(run(upstream, {"prompt": "Analyze shipments for the last 8 months", "filters": PRESET_7}),
                 422, "report_date_range_exceeded")
    assert body["message"] == ("You can generate reports for up to 6 months at a time. Please select a shorter "
                               "date range.")
    assert body["details"]["suggested_range"] == "last_6_months"
    assert upstream.requests == [] and ai.calls == 0
    [record] = report_activity_audit
    assert (record["outcome"], record["processing_stage"], record["date_validation_result"]) == (
        "failed", "date_resolution", "exceeded")


def test_no_shipments_in_the_resolved_period(run, ai):
    upstream = Upstream([])
    body = error(run(upstream, {"prompt": "last 30 days status", "filters": PRESET_7}), 422, "no_matching_records")
    assert len(upstream.requests) == 1 and ai.calls == 0


def test_exports_print_the_resolved_period(run, ai):
    from docx import Document
    from app.generators.docx_generator import generate_docx
    from app.models.report_schema import ReportResponse
    response = run(Upstream(manifest_rows(8)), {"prompt": "status breakdown for 30 days", "filters": PRESET_7})
    report = ReportResponse.model_validate(response.json())
    first = (datetime.now(IST).date() - timedelta(days=29)).isoformat()
    text = "\n".join(p.text for p in Document(io.BytesIO(generate_docx(report))).paragraphs)
    assert f"{first} to {datetime.now(IST).date().isoformat()}" in text
    from app.generators.pptx_generator import generate_pptx
    from pptx import Presentation
    deck = Presentation(io.BytesIO(generate_pptx(report)))
    assert any(first in shape.text_frame.text for slide in deck.slides for shape in slide.shapes if shape.has_text_frame)


# -- privacy boundary in the real report flow ----------------------------------------------------

PII = ("Rahul Sharma", "9876501234", "rahul.private@example.com", "12 MG Road, Andheri East", "Aarav Deshmukh")


def guarded(planned):
    """A real DeepSeekService (so the real privacy guard runs) whose SDK client is answered by ``planned``."""
    sent = []

    async def create(**kwargs):
        sent.append(copy.deepcopy(kwargs["messages"]))
        result = await planned.generate_json(kwargs["messages"])
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason="stop",
                                                                    message=SimpleNamespace(content=json.dumps(result)))])

    service = DeepSeekService(replace(settings, deepseek_api_key="synthetic-test-key"))
    service._get_client = lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return service, sent


def run_planned(planned, upstream, body):
    ai, sent = guarded(planned)
    cfg = replace(settings, shipment_api_base_url="https://shipments.test", report_ai_planner=True,
                  deepseek_api_key="synthetic-test-key")
    service = ReportService(ai, config=cfg, source=ShipmentSource(cfg, transport=httpx.MockTransport(upstream)))
    app.dependency_overrides[get_report_service] = lambda: service
    try:
        with TestClient(app) as client:
            response = client.post("/api/reports/generate", headers=HEADERS, json={
                "report_type": "shipment", "source": "shipment_api", **body})
    finally:
        app.dependency_overrides.clear()
    return response, sent


def pii_rows(n):
    return manifest_rows(n, consigneeName=PII[0], consigneePhone=PII[1], consigneeEmail=PII[2],
                         consigneeAddress=PII[3], sellerName=PII[4])


def test_production_title_no_longer_blocks_and_no_pii_reaches_the_provider(caplog):
    """The reported failure: a planner title "Delivery Rate vs RTO Rate" was blocked as a residual person."""
    planned = PlannedAI([section("delivery_vs_rto", ("status_category",), title="Delivery Rate vs RTO Rate"),
                         section("courier_split", ("courier",), title="Manifest Date Courier Breakdown")])
    response, sent = run_planned(planned, Upstream(pii_rows(10)), {
        "prompt": "Create insight report, comparison between RTO Transit and delivered shipment for 30 days.",
        "filters": {**PRESET_7, "status": "All Statuses"}})
    assert response.status_code == 200, response.text
    assert planned.events == ["plan", "narrative"]
    outbound = json.dumps(sent, ensure_ascii=False)
    assert not any(value in outbound for value in PII) and TOKEN not in outbound and "102795" not in outbound
    narrative_input = json.loads(sent[1][-1]["content"])
    assert narrative_input["approved_ordered_plan"][0]["title"] == "Delivery Rate vs RTO Rate"
    plan_input = json.loads(sent[0][-1]["content"])
    assert plan_input["resolved_period"]["label"] == "Last 30 days"
    assert response.json()["structured_report"]["scope"]["date_range"]["source"] == "prompt"
    assert "blocked" not in caplog.text


def test_a_failed_protection_returns_a_generic_error_and_logs_the_internal_reason(caplog, monkeypatch):
    planned = PlannedAI([section("delivery_vs_rto", ("status_category",))])

    class Unstable:      # the name model keeps finding a new person-like span: masking can never converge
        def __call__(self, text):
            spans = [SimpleNamespace(start_char=len(text) - 1, end_char=len(text), text=text[-1:], label_="PERSON")]
            return SimpleNamespace(ents=spans if text.strip() else [])

    monkeypatch.setattr(privacy, "_name_detector", lambda: Unstable())
    response, sent = run_planned(planned, Upstream(pii_rows(4)), {"prompt": "last 30 days status", "filters": PRESET_7})
    assert response.status_code == 422 and sent == []
    body = response.json()["error"]
    assert body["code"] == "ai_privacy_blocked" and body["message"] == privacy.PUBLIC_MESSAGE
    assert body["request_id"] == response.headers["x-request-id"]
    lowered = response.text.casefold()
    assert not any(word in lowered for word in ("residual", "spacy", "detector", "redact", "person", "inspection"))
    event = json.loads(next(r.getMessage() for r in caplog.records if r.name == "ai_box.security.privacy"
                            and '"blocked"' in r.getMessage()))
    assert event["detail"] == "residual_person" and event["stage"] == "report_plan"
    assert not any(value in caplog.text for value in PII)
