"""Source gate: selected filters reach the report, expected = fetched = analysed, conflicts are clarified."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.services.report_service import export_filename
from tests.test_dynamic_reports import ai, generate, make_orders, payload  # noqa: F401  (fixtures)
from tests.test_reports import client, fake_ai, isolated_validation_audit  # noqa: F401  (fixtures)

SEPTEMBER = {"from_date": "2026-09-01", "to_date": "2026-09-30"}


def test_complete_source_is_reconciled_and_reported(client, ai, coverage_audit):
    orders = make_orders(143)
    report = generate(client, "Shipment status overview", payload(orders, total=143, report_filter=SEPTEMBER))
    completeness = coverage_audit[-1]["completeness"]
    assert completeness["status"] == "complete"
    assert (completeness["expected_records"], completeness["fetched_records"], completeness["analyzed_records"]) == (143, 143, 143)
    assert report["records_analyzed"] == 143


def test_first_page_only_is_an_explicit_failure(client, ai):
    body = generate(client, "Shipment status overview", payload(make_orders(100), total=143, report_filter=SEPTEMBER),
                    status=422)
    assert body["error"]["code"] == "incomplete_source"
    assert "100 of the 143" in body["error"]["message"]
    assert body["error"]["details"]["missing_records"] == 43
    assert ai.calls == 0  # nothing was analysed or sent to DeepSeek


def test_overlapping_pages_are_deduplicated(client, ai, coverage_audit):
    orders = make_orders(143)
    report = generate(client, "Shipment status overview", payload(orders + orders[93:100], total=143, report_filter=SEPTEMBER))
    completeness = coverage_audit[-1]["completeness"]
    assert completeness["fetched_records"] == 150 and completeness["duplicate_records_removed"] == 7
    assert completeness["status"] == "complete" and report["records_analyzed"] == 143


def test_conflicting_versions_stop_or_count_once(client, ai, coverage_audit):
    orders = make_orders(10)
    changed = {**orders[0], "status": "Cancelled"}
    body = generate(client, "Shipment status overview", payload(orders + [changed], total=11), status=422)
    assert body["error"]["code"] == "conflicting_records" and body["error"]["details"]["conflicting_duplicates"] == 1
    report = generate(client, "Shipment status overview", payload(orders + [changed], total=11), allow_partial=True)
    assert report["records_analyzed"] == 10  # one shipment, never double-counted
    assert any("first version of each was used" in n for n in coverage_audit[-1]["notes"])


def test_nine_digit_order_ids_are_distinct_shipments(client, ai, coverage_audit):
    orders = make_orders(5)
    for i, order in enumerate(orders):
        order["order_id"] = str(563583354 + i)  # looks like a phone number to the PII scrubber
    report = generate(client, "Shipment status overview", payload(orders, total=5))
    completeness = coverage_audit[-1]["completeness"]
    assert completeness["unique_records"] == 5 and completeness["conflicting_duplicates"] == 0


def test_selected_status_must_match_the_records(client, ai):
    data = payload(make_orders(9), total=9, report_filter={**SEPTEMBER, "status_label": "Delivered"})
    body = generate(client, "Shipment overview", data, status=422)
    assert body["error"]["code"] == "scope_mismatch"
    assert body["error"]["details"]["outside_selected_status"] == 6


def test_explicit_filters_use_local_calendar_days(client, ai, coverage_audit):
    orders = make_orders(3)
    orders[0]["orderDate"] = "2026-08-31T19:00:00Z"   # 1 Sep 00:30 IST: inside
    orders[1]["orderDate"] = "2026-09-30T19:00:00Z"   # 1 Oct 00:30 IST: outside
    filters = {"from_date": "2026-09-01", "to_date": "2026-09-30", "timezone": "Asia/Kolkata"}
    body = generate(client, "Shipment status overview", payload(orders, total=3), status=422, filters=filters)
    assert body["error"]["details"]["outside_selected_period"] == 1
    report = generate(client, "Shipment status overview", payload(orders, total=3), filters=filters, allow_partial=True)
    assert report["records_analyzed"] == 2
    assert coverage_audit[-1]["completeness"]["selected_filters"]["from_date"] == "2026-09-01"


def test_nothing_in_scope_is_an_honest_empty_state(client, ai):
    orders = make_orders(4, day=lambda i: date(2026, 7, 5))
    body = generate(client, "Shipment status overview", payload(orders, total=4, report_filter=SEPTEMBER), status=422,
                    allow_partial=True)
    assert body["error"]["code"] == "no_matching_records"


def test_prompt_period_outside_the_selection_asks_for_clarification(client, ai):
    data = payload(make_orders(20), total=20, report_filter=SEPTEMBER, as_of="2026-10-13")
    body = generate(client, "Courier performance for last week", data, status=422)
    assert body["error"]["code"] == "clarification_required"
    assert body["error"]["details"]["conflict"] == "period"
    assert ai.calls == 0


def test_prompt_status_conflicting_with_selection_asks_for_clarification(client, ai):
    orders = make_orders(6, statuses=("Delivered",))
    data = payload(orders, total=6, report_filter={**SEPTEMBER, "status_label": "Delivered"})
    body = generate(client, "Show cancelled shipments by courier", data, status=422)
    assert body["error"]["code"] == "clarification_required"
    assert body["error"]["details"]["conflict"] == "status"


def test_ai_receives_selected_filters_and_cohort_caveat(client, ai):
    orders = make_orders(12, statuses=("Delivered",))
    data = payload(orders, total=12, report_filter={**SEPTEMBER, "status_label": "Delivered"})
    generate(client, "Create a detailed management report with recommendations", data)
    system = ai.messages[0]["content"]
    assert "Selected filters: order date 2026-09-01 to 2026-09-30" in system and "status Delivered" in system
    assert "source matched 12 records" in system
    assert "cannot establish account-wide delivery, RTO or cancellation rates" in system


@pytest.mark.parametrize(("seller", "name", "expected"), [
    ("Acme Traders Pvt. Ltd.", "Delivered shipments, 1 Sep 2026 – 30 Sep 2026",
     "Bigship_Acme-Traders-Pvt-Ltd_Delivered-shipments-1-Sep-2026-30-Sep-2026_2026-10-01.pdf"),
    (None, '../../etc/passwd"; x', "Bigship_Seller_etc-passwd-x_2026-10-01.pdf"),
    ("", "", "Bigship_Seller_Report_2026-10-01.pdf"),
])
def test_export_filename_is_sanitized(seller, name, expected):
    # 30 Sep 20:00 UTC is 1 Oct in India.
    assert export_filename(seller, name, datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc), "pdf") == expected
