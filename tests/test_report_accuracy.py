"""Synthetic request-local accuracy, privacy and revision regressions."""
import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date

import pytest

from app.analysis.accuracy import (DatasetSnapshot, check_table, Audit, reference_row,
                                   repair_coverage, validate_final, verify_revision)
from app.analysis.completeness import SelectedFilters, check_source
from app.analysis.plan import AnalysisPlan, Filter, TableSpec
from app.charts.fields import get_adapter
from app.errors import ReportAccuracyError, ReportRevisionError, request_id_var
from app.models.report_schema import ReportDocument, ReportResponse, ReportTable, DisplayMetric
from tests.test_reports import client, fake_ai, isolated_validation_audit


def setup_report(n=4, plan=None):
    records = [{"order_id": f"SYNTH-{i}", "status": "Delivered" if i % 2 == 0 else "RTO Delivered",
                "courierName": "Synthetic A" if i % 2 == 0 else "Synthetic B",
                "orderDate": "2026-09-01", "totalCharge": str(100 + i), "invoiceTotalAmount": "1000",
                "consignee": {"city": "Synthetic City", "state": "Synthetic State"},
                "domestic_transit_stats": {"date_pickup": "2026-09-02", "date_delivery": "2026-09-04",
                                           "courier_remarks": "Delivered"}} for i in range(n)]
    data = {"orders": records}
    source = check_source(data, SelectedFilters(), expected_records=n)
    snapshot = DatasetSnapshot.capture(data, source, user_id="synthetic-account", prompt="Synthetic report")
    count = n // 2 + n % 2 if plan and plan.filters else n
    report = ReportResponse.model_validate({
        "report_id": "synthetic-report", "ai_model": "test", "report_type": "shipment", "report_name": "Shipments",
        "generated_at": "2026-10-21T10:00:00Z", "records_analyzed": count,
        "content": {"title": "Shipments", "executive_summary": "The supplied records were reviewed."},
        "structured_report": {"report_id": "synthetic-report", "report_type": "shipment", "title": "Shipments",
            "subtitle": "Report", "generated_at": "2026-10-21T10:00:00Z", "executive_summary": {"paragraphs": ["Shipment analysis."]},
            "metrics": [], "sections": [], "findings": [], "recommendations": [], "limitations": [], "scope": {}},
    })
    return records, snapshot, report


def events(caplog):
    return [r.report_check for r in caplog.records if hasattr(r, "report_check")]


def test_246_rows_reported_as_251_blocks_with_safe_error(caplog):
    _, snapshot, report = setup_report(246)
    report.records_analyzed = 251
    with pytest.raises(ReportAccuracyError) as caught:
        validate_final(report, snapshot)
    assert caught.value.details == {"request_id": snapshot.request_id}
    assert any(c["check_id"] == "analysis_subset" and c["status"] == "FAIL" for c in events(caplog)[0]["checks"])


def test_invented_ups_category_blocks_without_logging_category_name(caplog):
    _, snapshot, report = setup_report()
    report.tables = [ReportTable(title="Couriers", columns=["Courier", "Orders"], rows=[["UPS", "4"]])]
    with pytest.raises(ReportAccuracyError):
        validate_final(report, snapshot)
    assert "UPS" not in caplog.text
    assert any(c["check_id"] == "table_category" and c["status"] == "FAIL" for c in events(caplog)[0]["checks"])


def test_wrong_median_blocks():
    _, snapshot, report = setup_report()
    report.insights = {"kpis": {"median_charge": 999}}
    with pytest.raises(ReportAccuracyError):
        validate_final(report, snapshot)
    report.insights["kpis"]["median_charge"] = 102  # documented upper-middle v3 median
    validate_final(report, snapshot)


def test_wrong_percentage_denominator_blocks():
    _, snapshot, report = setup_report()
    report.metrics = {"status_category_breakdown": [
        {"category": "delivered", "count": 2, "percent": 40}, {"category": "rto", "count": 2, "percent": 40}]}
    with pytest.raises(ReportAccuracyError):
        validate_final(report, snapshot)


def test_audit_only_keeps_failures_but_allows_report(caplog):
    _, snapshot, report = setup_report()
    report.insights = {"kpis": {"median_charge": 999}}
    assert validate_final(report, snapshot, block_on_failure=False) is report
    event = events(caplog)[0]
    assert event["failed"] > 0 and event["blocking"] is False
    assert "delivery=allowed policy=audit_only" in caplog.text
    assert verify_revision(report)["report_id"] == report.report_id


@pytest.mark.parametrize("strict, expected_status", [(False, 200), (True, 422)])
def test_backend_accuracy_policy_controls_delivery(client, fake_ai, monkeypatch, strict, expected_status):
    from app.api.report_routes import get_report_service
    from app.config import settings
    from app.main import app
    from app.services.report_service import ReportService
    records, _, _ = setup_report()
    fake_ai.response = {"title": "Shipments", "executive_summary": {"paragraphs": ["Shipment review."]}}
    original = ReportService._verify
    def introduce_mismatch(self, report, *args, **kwargs):
        original(self, report, *args, **kwargs)
        report.records_analyzed = 251
    monkeypatch.setattr(ReportService, "_verify", introduce_mismatch)
    app.dependency_overrides[get_report_service] = lambda: ReportService(fake_ai, config=replace(settings, report_accuracy_strict=strict))
    response = client.post("/api/reports/generate", json={"report_type": "shipment", "prompt": "Shipment status overview",
                                                         "data": {"orders": records}, "expected_records": len(records)})
    assert response.status_code == expected_status, response.text
    if strict:
        assert response.json()["error"]["code"] == "report_accuracy_failed"
    else:
        assert response.json()["validation_token"]


def test_malformed_final_component_returns_structured_accuracy_error():
    _, snapshot, report = setup_report()
    report.metrics = {"status_category_breakdown": [{"count": "bad"}]}
    with pytest.raises(ReportAccuracyError):
        validate_final(report, snapshot)


def test_delivered_subset_is_valid_but_all_statuses_label_is_not():
    plan = AnalysisPlan(filters=(Filter("status_category", "eq", "delivered"),))
    _, snapshot, report = setup_report(246, plan)
    report.structured_report.metrics = [DisplayMetric(key="orders", label="Delivered Shipments", value=123, unit="number")]
    validate_final(report, snapshot, plan=plan)
    report.structured_report.metrics[0].label = "Shipments (All Statuses)"
    with pytest.raises(ReportAccuracyError):
        validate_final(report, snapshot, plan=plan)


def test_states_cannot_substitute_for_requested_cities_and_repair_revalidates(caplog):
    caplog.set_level(logging.INFO)
    _, snapshot, report = setup_report()
    plan = AnalysisPlan(tables=(TableSpec("grouped", "Destination cities", ("orders",), group_by="destination_city"),))
    report.tables = [ReportTable(title="States", columns=["State", "Orders"], rows=[["Synthetic State", "4"]])]
    validate_final(report, snapshot, plan=plan)
    assert any(c["check_id"] == "prompt_coverage_destination_city" and c["actual"] == "missing" for c in events(caplog)[0]["checks"])
    repair_coverage(report, snapshot, plan, get_adapter("shipment"))
    validate_final(report, snapshot, plan=plan)
    assert any(t.columns[0] == "Destination City" for t in report.tables)


def test_missing_timelines_are_detected_then_rebuilt(caplog):
    caplog.set_level(logging.INFO)
    _, snapshot, report = setup_report()
    plan = AnalysisPlan(tables=(TableSpec("duration", "Timelines", group_by="courier"),))
    validate_final(report, snapshot, plan=plan)
    assert any(c["check_id"] == "delivery_timeline_coverage" and c["status"] == "WARN" for c in events(caplog)[0]["checks"])
    repair_coverage(report, snapshot, plan, get_adapter("shipment"))
    validate_final(report, snapshot, plan=plan)
    assert next(t for t in report.tables if t.id == "plan_duration").values[0][1:] == [2, 3, 3, 3]


def test_missing_nested_field_mapping_is_detected():
    _, snapshot, report = setup_report()
    adapter = get_adapter("shipment")
    normalized = adapter.rows(json.loads(snapshot.data_json))
    assert normalized[0]["destination_city"] == "Synthetic City"
    assert normalized[0]["pickup_date"] == date(2026, 9, 2)
    normalized[0]["pickup_date"] = None
    with pytest.raises(ReportAccuracyError):
        validate_final(report, snapshot, normalized_rows=normalized)


def test_valid_top_n_and_other_bucket_do_not_need_to_equal_cohort_alone():
    records, snapshot, report = setup_report(6)
    records[0]["courierName"] = "Synthetic C"
    records[2]["courierName"] = "Synthetic C"
    snapshot = DatasetSnapshot.capture({"orders": records})
    report.tables = [ReportTable(title="Top couriers", columns=["Courier", "Orders"],
                                rows=[["Synthetic B", "3"], ["Synthetic C", "2"]], total_rows=3, truncated=True)]
    validate_final(report, snapshot)
    report.tables[0].rows.append(["Other", "1"])
    validate_final(report, snapshot)
    report.tables[0].rows[-1][1] = "2"
    with pytest.raises(ReportAccuracyError):
        validate_final(report, snapshot)


def test_manifest_unavailable_is_warning_and_never_checked_against_order_date(caplog):
    caplog.set_level(logging.INFO)
    records, _, report = setup_report()
    filters = SelectedFilters(date(2026, 9, 1), date(2026, 9, 30), date_field="manifest_datetime")
    data = {"orders": records}
    source = check_source(data, filters, expected_records=4, source_owned=True)
    snapshot = DatasetSnapshot.capture(data, source, filters)
    report.structured_report.scope["selected_period"] = filters.to_dict()
    validate_final(report, snapshot)
    check = next(c for c in events(caplog)[0]["checks"] if c["check_id"] == "selected_date_scope")
    assert check["status"] == "WARN" and check["expected"] == "manifest_datetime"


def test_concurrent_request_logs_are_single_records_and_isolated(caplog):
    caplog.set_level(logging.INFO)
    def work(i):
        token = request_id_var.set(f"synthetic-request-{i}")
        try:
            _, snapshot, report = setup_report(i + 2)
            report.report_id = f"report-{i}"
            validate_final(report, snapshot)
            return snapshot.fingerprint
        finally:
            request_id_var.reset(token)
    with ThreadPoolExecutor(max_workers=4) as pool:
        fingerprints = list(pool.map(work, range(4)))
    records = events(caplog)
    assert len(records) == 4
    for item in records:
        index = int(item["request_id"].rsplit("-", 1)[1])
        assert item["report_id"] == f"report-{index}" and item["dataset_fingerprint"] == fingerprints[index]
    assert all(r.message.count("[REPORT CHECK]") == 1 and r.message.count("[RESULT]") == 1
               for r in caplog.records if hasattr(r, "report_check"))


def test_response_and_export_share_signed_revision_and_tampering_is_rejected():
    _, snapshot, report = setup_report()
    validate_final(report, snapshot)
    public = report.model_dump(mode="json")
    assert "verification" not in public["structured_report"]
    document = ReportDocument.model_validate(public)
    assert verify_revision(document) == verify_revision(report)
    document.records_analyzed += 1
    with pytest.raises(ReportRevisionError):
        verify_revision(document)


def test_explicit_final_narrative_count_is_independently_checked():
    _, snapshot, report = setup_report(246)
    report.content.executive_summary = "251 shipments were analysed."
    with pytest.raises(ReportAccuracyError):
        validate_final(report, snapshot)


def test_missing_money_is_never_zero():
    records, _, report = setup_report()
    for record in records:
        del record["totalCharge"]
    snapshot = DatasetSnapshot.capture({"orders": records})
    report.insights = {"kpis": {"spend": 0}}
    with pytest.raises(ReportAccuracyError):
        validate_final(report, snapshot)


def test_rto_reason_is_never_cancellation_reason():
    row = reference_row({"status": "RTO", "orderCancelReason": "Cancellation only"})
    assert row["rto_reason"] is None
    row = reference_row({"status": "RTO", "rtoReason": "Return reason", "orderCancelReason": "Cancellation only"})
    assert row["rto_reason"] == "Return reason"


def test_snapshot_does_not_change_with_caller_mutation():
    records, snapshot, report = setup_report()
    records[0]["totalCharge"] = "999999"
    assert snapshot.records()[0]["totalCharge"] == "100"
    validate_final(report, snapshot)


def test_working_source_rows_are_read_only():
    from app.analysis.accuracy import immutable_data
    data = immutable_data({"orders": [{"totalCharge": "100"}]})
    with pytest.raises(TypeError):
        data["orders"][0]["totalCharge"] = "999"
    with pytest.raises(TypeError):
        data["orders"].append({})
    assert json.loads(json.dumps(data))["orders"][0]["totalCharge"] == "100"


def test_ascii_terminal_fallback(monkeypatch, caplog):
    from types import SimpleNamespace
    from app.analysis import accuracy
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(accuracy.sys, "stderr", SimpleNamespace(encoding="ascii"))
    _, snapshot, report = setup_report()
    validate_final(report, snapshot)
    assert "[PASS]" in caplog.text and "[WARN]" in caplog.text


def test_api_response_and_pdf_word_exports_use_the_same_revision_without_refetch(client, fake_ai, monkeypatch):
    from app.services.shipment_source import ShipmentSource
    records, _, _ = setup_report()
    fake_ai.response = {"title": "Shipment report", "executive_summary": {"paragraphs": ["The supplied shipments were reviewed."]}}
    response = client.post("/api/reports/generate", json={"report_type": "shipment", "prompt": "Shipment status overview",
                                                         "data": {"orders": records}, "expected_records": len(records)})
    assert response.status_code == 200, response.text
    public = response.json()
    receipt = verify_revision(ReportResponse.model_validate(public))
    assert receipt["request_id"] == response.headers["X-Request-Id"]
    async def forbidden_fetch(*args, **kwargs):
        raise AssertionError("Exports must not fetch shipments")
    monkeypatch.setattr(ShipmentSource, "fetch", forbidden_fetch)
    for fmt, magic in (("pdf", b"%PDF"), ("docx", b"PK")):
        exported = client.post("/api/reports/export/" + fmt, json=public)
        assert exported.status_code == 200, exported.text
        assert exported.content.startswith(magic)
    public["records_analyzed"] += 1
    assert client.post("/api/reports/export/pdf", json=public).status_code == 409


def test_full_response_metrics_are_bound_to_receipt():
    _, snapshot, report = setup_report()
    validate_final(report, snapshot)
    report.metrics["invented"] = 251
    with pytest.raises(ReportRevisionError):
        verify_revision(report)


def test_nested_consignee_destination_wins_over_unrelated_root_fields():
    from app.prompts.shipment_prompt import parse_order
    record = {"status": "Delivered", "city": "Origin city", "state": "Origin state",
              "consignee": {"city": "Destination city", "state": "Destination state"}}
    parsed = parse_order(record)
    assert parsed.destination_city == "Destination city" and parsed.destination_state == "Destination state"
