"""Selected preset -> exact upstream range -> report and exported header."""
from datetime import date, datetime, time, timedelta, timezone
from io import BytesIO
from zoneinfo import ZoneInfo

import pytest
from docx import Document
from pypdf import PdfReader

from app.analysis.completeness import selected_filters
from app.models.report_schema import ReportFilters, ReportRequest, ReportDocument
from app.report_period import last_days_period, local_today
from app.services.shipment_source import build_query
from app.generators.pdf_generator import generate_pdf
from app.generators.docx_generator import generate_docx
from tests.test_shipment_source import run, ai, Upstream, rows, isolated_validation_audit


@pytest.mark.parametrize("days", [1, 7, 30, 60])
def test_last_n_calendar_days_include_today_exactly(days):
    start, end = last_days_period(days, "Asia/Kolkata", today=date(2026, 10, 21))
    assert end == date(2026, 10, 21) and (end - start).days + 1 == days
    if days == 60:
        assert start == date(2026, 8, 23)


@pytest.mark.parametrize("label", ["Last 60 Days", "last_60_days", "last60"])
def test_ui_preset_replaces_stale_explicit_dates_once(label, monkeypatch):
    monkeypatch.setattr("app.report_period.local_today", lambda zone: date(2026, 10, 21))
    filters = ReportFilters(date_field="manifest_datetime", date_range=label,
                            from_date="2023-05-17", to_date="2026-10-19")
    assert filters.from_date == date(2026, 8, 23) and filters.to_date == date(2026, 10, 21)
    query = build_query(filters, today=date(2026, 10, 21))
    assert query.from_utc == "2026-08-22 18:30:00"
    assert query.to_utc == "2026-10-21 18:30:00"  # exclusive next local midnight
    assert query.date_filter_type == "manifest_datetime"


def test_custom_dates_remain_exact_and_conflicting_presets_are_rejected():
    filters = ReportFilters(date_range="custom", from_date="2026-02-01", to_date="2026-02-04")
    assert filters.from_date == date(2026, 2, 1) and filters.to_date == date(2026, 2, 4)
    with pytest.raises(ValueError, match="same number"):
        ReportFilters(range_days=60, date_range="Last 30 Days")


def test_top_level_and_legacy_presets_reach_explicit_source_filters(monkeypatch):
    monkeypatch.setattr("app.report_period.local_today", lambda zone: date(2026, 10, 21))
    request = ReportRequest.model_validate({"report_type": "shipment", "source": "shipment_api",
        "date_range": "last60", "filters": {"date_field": "manifest_datetime"}})
    assert request.filters.from_date == date(2026, 8, 23)
    request = ReportRequest.model_validate({"report_type": "shipment", "data": {
        "orders": [{"order_id": "synthetic"}], "report_filter": {
            "date_range": "last60", "date_field": "manifest_datetime", "status_label": "Delivered"}}})
    assert request.filters.from_date == date(2026, 8, 23)
    assert request.filters.date_field == "manifest_datetime" and request.filters.status == "Delivered"


def test_legacy_filter_keeps_the_selected_date_field_and_timezone():
    selected = selected_filters(None, {"orders": [{}], "report_filter": {
        "date_field": "manifest_datetime", "timezone": "Asia/Kolkata",
        "from_date": "2026-08-23", "to_date": "2026-10-21"}})
    assert selected.date_field == "manifest_datetime" and selected.timezone == "Asia/Kolkata"


class DateFilteredUpstream(Upstream):
    def __call__(self, request):
        import json
        payload = json.loads(request.content)
        start = datetime.fromisoformat(payload["fromDate"]).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(payload["toDate"]).replace(tzinfo=timezone.utc)
        original = self.data
        self.data = [r for r in original if start <= datetime.fromisoformat(r["manifest_datetime"]) < end]
        try:
            return super().__call__(request)
        finally:
            self.data = original


def test_60_days_fetch_and_display_selected_manifest_period_in_json_pdf_word(run):
    start, end = last_days_period(60, "Asia/Kolkata")
    zone = ZoneInfo("Asia/Kolkata")
    first = datetime.combine(start, time.min, zone)
    exclusive_end = datetime.combine(end + timedelta(days=1), time.min, zone)
    data = rows(4)
    for record, moment in zip(data, [first - timedelta(seconds=1), first,
                                    exclusive_end - timedelta(seconds=1), exclusive_end]):
        record["manifest_datetime"] = moment.isoformat()
        record["orderDate"] = "2023-05-17"  # unrelated observed order dates must not replace the selection
    upstream = DateFilteredUpstream(data)
    response = run(upstream, body={"filters": {"date_field": "manifest_datetime", "range_days": 60,
        "from_date": "2023-05-17", "to_date": "2023-05-18", "timezone": "Asia/Kolkata", "status": "All Statuses"}})
    assert response.status_code == 200, response.text
    public = response.json()
    assert public["records_analyzed"] == 2
    scope = public["structured_report"]["scope"]
    assert scope["selected_period"]["from_date"] == start.isoformat()
    assert scope["requested_period"]["to"] == end.isoformat()
    assert scope["observed_data_period"] == {"from": "2023-05-17", "to": "2023-05-17"}
    assert public["insights"]["scope"]["from_date"] == start.isoformat()
    for request in upstream.requests:
        assert request["date_filter_type"] == "manifest_datetime"
        assert request["fromDate"] == first.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        assert request["toDate"] == exclusive_end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    document = ReportDocument.model_validate(public)
    pdf = "\n".join(p.extract_text() for p in PdfReader(BytesIO(generate_pdf(document))).pages)
    word = "\n".join(p.text for p in Document(BytesIO(generate_docx(document))).paragraphs)
    for text in (pdf, word):
        assert "Period:" in text and start.isoformat() in text and end.isoformat() in text
        assert "manifest datetime" in text
