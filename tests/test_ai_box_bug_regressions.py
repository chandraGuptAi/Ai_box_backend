"""Regression tests for the six AI Box report bugs reported on 27 Oct 2026 (synthetic data only).

BUG 1  "No shipments match" although a matching shipment was received
BUG 2  "AI backend unreachable": sanitized, retryable errors and a readiness check
BUG 3  "RTO Orders Overview: Orders" instead of "Total Shipments"
BUG 4  Repeated freight KPI labels and no rupee formatting
BUG 5  Pre-analysis count vs report count; an order ID query became an all-shipment report
BUG 6  A weight / courier question failed with "Unable to generate report"
"""
import asyncio
import copy
import json
from dataclasses import replace
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.errors import NoMatchingRecordsError, UpstreamTimeoutError
from app.models.report_schema import ReportRequest
from app.services.report_service import ReportService
from app.services.shipment_source import ShipmentSource
from tests.test_ordered_reports import PlannedAI, section
from tests.test_shipment_source import TOKEN, Upstream

FILTERS = {"date_field": "manifest_datetime", "from_date": "2026-10-21", "to_date": "2026-10-27", "status": "All Statuses",
           "timezone": "Asia/Kolkata"}


def shipment(order_id, status="Pickup Scheduled", pincode="400000", courier="Delhivery Surface", charge="100.00",
             weight=1.0, day="2026-10-24"):
    return {"order_id": order_id, "awb_number": f"AWB{order_id}", "status": status, "courierName": courier,
            "consignee": {"id": f"B-{order_id}", "city": "Mumbai", "pincode": pincode, "state": "Maharashtra"},
            "orderDate": day, "manifest_datetime": f"{day}T10:00:00+05:30", "totalCharge": charge,
            "invoiceTotalAmount": "1000", "weight": weight}


def run(prompt, records, sections, cohort=(), status="All Statuses", title=None, ai=None, **options):
    """Payload mode through the production plan-first workflow."""
    def alter(plan):
        plan = {**plan, "cohort": list(cohort)}
        return {**plan, "report_title": title} if title else plan
    ai = ai or PlannedAI(sections, alter_plan=alter)
    request = ReportRequest.model_validate({"report_type": "shipment", "prompt": prompt, "data": {"orders": records},
        "expected_records": len(records), "filters": {**FILTERS, "status": status}})
    config = replace(settings, report_ai_planner=True, report_accuracy_strict=True, **options)
    return asyncio.run(ReportService(ai, config=config).generate_report(request, user_id="seller-a")), ai


def via_api(prompt, records, sections, cohort=(), status="All Statuses", upstream=None):
    """Backend-owned fetch from a mocked shipment API (the production source)."""
    upstream = upstream or Upstream(records)
    ai = PlannedAI(sections, alter_plan=lambda p: {**p, "cohort": list(cohort)})
    cfg = replace(settings, report_ai_planner=True, report_accuracy_strict=True, shipment_api_base_url="https://shipments.test")
    request = ReportRequest.model_validate({"report_type": "shipment", "source": "shipment_api", "prompt": prompt,
                                           "filters": {**FILTERS, "status": status}})
    service = ReportService(ai, config=cfg, source=ShipmentSource(cfg, transport=httpx.MockTransport(upstream)))
    return asyncio.run(service.generate_report(request, shipment_credential=TOKEN, user_id="seller-a")), upstream


def cards(report):
    return [(c.label, c.value) for c in report.structured_report.metrics]


# --- BUG 1 --------------------------------------------------------------------------------------------------

PIN_SECTIONS = [section("pincode_report", ("destination_pincode",), ("orders",))]


@pytest.mark.parametrize("status_words", ["Pending", "pickup_scheduled", "PICKUP SCHEDULED", "Pickup Scheduled"])
def test_bug1_one_pickup_scheduled_shipment_is_found_whatever_words_the_plan_uses(status_words):
    cohort = [{"field": "destination_pincode", "op": "in", "value": ["400000"]},
              {"field": "shipment_status", "op": "eq", "value": status_words}]
    report, _ = via_api("Generate pincode Report of Pincode 400000", [shipment("1001")], PIN_SECTIONS, cohort,
                        status="Pickup Scheduled")
    assert report.records_analyzed == 1
    assert report.tables[0].values == [["400000", 1]]
    assert cards(report) == [("Total Shipments", 1)]


def test_bug1_pincode_from_the_prompt_is_applied_even_when_the_plan_forgets_it():
    records = [shipment("1001"), shipment("1002", pincode="110001")]
    report, _ = run("Generate pincode Report of Pincode 400000", records, PIN_SECTIONS)
    assert report.records_analyzed == 1


def test_bug1_genuine_zero_says_which_condition_removed_the_shipments():
    with pytest.raises(NoMatchingRecordsError) as error:
        via_api("Generate pincode Report of Pincode 400001", [shipment("1001")], PIN_SECTIONS, status="Pickup Scheduled")
    assert error.value.code == "no_matching_records"
    assert error.value.message == (
        "You have 1 shipment with status Pickup Scheduled between 21 Oct 2026 and 27 Oct 2026, but none of them have "
        "Destination Pincode 400001. Check this, or remove it from your question to see all your shipments.")
    assert error.value.details["filter_field"] == "destination_pincode"
    assert error.value.details["source_records"] == 1


def test_bug1_no_shipments_at_all_is_a_plain_message():
    with pytest.raises(NoMatchingRecordsError) as error:
        via_api("Generate pincode Report of Pincode 400000", [], PIN_SECTIONS, status="Pickup Scheduled")
    assert error.value.message == ("We didn't find any shipments with status Pickup Scheduled between 21 Oct 2026 and "
                                   "27 Oct 2026. Try selecting a longer date range or a different status.")


def test_bug1_partial_courier_name_means_the_existing_courier():
    records = [shipment("1", courier="Delhivery Surface"), shipment("2", courier="Blue Dart")]
    cohort = [{"field": "courier", "op": "eq", "value": "delhivery"}]
    report, _ = run("Delhivery shipments", records, [section("by_courier", ("courier",))], cohort)
    assert report.records_analyzed == 1 and report.tables[0].values == [["Delhivery Surface", 1]]


def test_bug1_messages_follow_the_question_language():
    from app.messages import language
    assert language("Pichhle hafte kitne shipment deliver hue?") == "hinglish"
    assert language("पिछले हफ़्ते कितने शिपमेंट डिलीवर हुए?") == "hi"
    assert language("How many shipments were delivered?") == "en"
    with pytest.raises(NoMatchingRecordsError) as error:
        via_api("pincode 400001 ki report banao", [shipment("1001")], PIN_SECTIONS)
    assert "lekin inmein se kisi mein Destination Pincode 400001 nahi hai" in error.value.message


# --- BUG 2 --------------------------------------------------------------------------------------------------

def test_bug2_upstream_timeout_is_retryable_and_sanitized():
    def slow(request):
        raise httpx.ReadTimeout("timed out reading https://shipments.test/internal", request=request)
    with pytest.raises(UpstreamTimeoutError) as error:
        via_api("Status breakdown", [shipment("1")], [section("statuses", ("status_category",))], upstream=slow)
    payload = error.value.to_payload()["error"]
    assert payload["retryable"] is True and error.value.status_code == 504
    assert "shipments.test" not in json.dumps(payload) and "internal" not in json.dumps(payload)


def test_bug2_readiness_reports_dependencies_without_internal_details(monkeypatch):
    from app.main import app
    client = TestClient(app)
    response = client.get("/health/ready")
    assert response.status_code == 200 and response.json()["checks"]["database"] is True
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody:secret@127.0.0.1:1/missing")
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["database"] is False
    assert "secret" not in response.text and "127.0.0.1" not in response.text


# --- BUG 3 / BUG 4 ------------------------------------------------------------------------------------------

def test_bug3_rto_report_cards_say_total_shipments_and_are_not_repeated():
    records = [shipment(str(i), status="Delivered") for i in range(24)]
    specs = [section("rto_orders_overview", (), ("rto_orders", "orders", "rto_rate")),
             section("rto_stage_breakdown", ("rto_stage",), ("rto_orders",))]
    report, _ = run("Kitne Order Mere RTO Hue or Kyu?", records, specs, title="RTO Orders Summary")
    assert cards(report) == [("RTO Orders", 0), ("Total Shipments", 24), ("RTO Rate", "0%")]
    assert [m.label for m in report.key_metrics] == ["RTO Orders", "Total Shipments", "RTO Rate"]


FREIGHT = [section("total_and_average_freight_charges", (), ("shipping_charges", "avg_charge", "orders"))]


def test_bug4_freight_cards_have_distinct_labels_and_rupee_values():
    charges = ["806.03"] * 234 + ["806.80"]
    records = [shipment(str(i), status="Delivered", charge=c) for i, c in enumerate(charges)]
    report, _ = run("Analyze shipments over the last 30 days and show total freight charges, average freight charge "
                    "per shipment", records, FREIGHT)
    # 234 x 806.03 + 806.80 = 189,417.82; / 235 = 806.0333 -> 806.03 (half-up to paise)
    assert cards(report) == [("Total Freight Charges", "₹1,89,417.82"),
                             ("Average Freight Charge per Shipment", "₹806.03"), ("Total Shipments", 235)]
    # The exact numbers stay available for clients and the accuracy check.
    metrics = report.metrics["section_metrics"]
    assert metrics["total_and_average_freight_charges.total.shipping_charges"] == 189417.82


@pytest.mark.parametrize("value, text", [(189416.82, "₹1,89,416.82"), (806.025, "₹806.03"), (0.005, "₹0.01"),
                                         (1234567.895, "₹12,34,567.90"), (100, "₹100.00"), (-1500.5, "-₹1,500.50")])
def test_bug4_inr_format_is_indian_grouping_rounded_half_up(value, text):
    from app.analysis.section_reports import format_inr
    assert format_inr(value) == text


def test_bug4_pdf_word_and_powerpoint_show_the_same_cards():
    from docx import Document
    from pptx import Presentation
    from pypdf import PdfReader
    from app.generators.docx_generator import generate_docx
    from app.generators.pdf_generator import generate_pdf
    records = [shipment(str(i), status="Delivered", charge="806.03") for i in range(3)]
    report, _ = run("Total and average freight charges", records, FREIGHT)
    service = ReportService(PlannedAI())
    pptx, _ = service.export_pptx(report)
    texts = {
        "pdf": " ".join(p.extract_text() for p in PdfReader(BytesIO(generate_pdf(report))).pages),
        "docx": " ".join([p.text for p in Document(BytesIO(generate_docx(report))).paragraphs] +
                         [c.text for t in Document(BytesIO(generate_docx(report))).tables for r in t.rows for c in r.cells]),
        "pptx": " ".join(shape.text_frame.text for slide in Presentation(BytesIO(pptx)).slides
                         for shape in slide.shapes if shape.has_text_frame) + " ".join(
                         c.text for slide in Presentation(BytesIO(pptx)).slides for shape in slide.shapes
                         if shape.has_table for r in shape.table.rows for c in r.cells),
    }
    for name, text in texts.items():
        flat = " ".join(text.split()).upper()
        assert "TOTAL FREIGHT CHARGES" in flat, name
        assert "₹2,418.09" in flat, name
        assert "TOTAL AND AVERAGE FREIGHT CHARGES: SHIPPING CHARGES" not in flat, name


# --- BUG 5 --------------------------------------------------------------------------------------------------

STATUS_SECTIONS = [section("shipment_status_for_the_requested_order", ("status_category",), charts=["bar"])]


def mixed_week():
    return ([shipment(str(296803800 + i), status="Cancelled") for i in range(21)]
            + [shipment(str(296803827), status="Pickup Scheduled")]
            + [shipment(str(296803900 + i), status="Pickup Scheduled") for i in range(3)])


@pytest.mark.parametrize("prompt", ["Order id :- 296803827 status ?", "Order ID: 296803827 – status?",
                                    "296803827 order no ka status batao", "order no. 296803827"])
def test_bug5_order_id_question_reports_only_that_order(prompt):
    report, ai = run(prompt, mixed_week(), STATUS_SECTIONS, title="Shipment Status for Order [REDACTED]")
    assert report.records_analyzed == 1
    assert report.tables[0].values == [["pending", 1]]
    assert report.report_name == "Shipment Status for Order 296803827"
    assert "296803827" not in json.dumps(ai.calls[1])  # the narrative call never receives the ID


def test_bug5_awb_typed_as_an_order_number_is_still_found():
    report, _ = run("order AWB296803827 ka status", mixed_week(), STATUS_SECTIONS)
    assert report.records_analyzed == 1


def test_bug5_awb_before_its_description_with_a_general_plan_reports_only_that_shipment():
    """27/28 Oct: the planner saw [REDACTED], asked a question and fell back to an overview of all 22 shipments."""
    records = mixed_week()
    records[5]["awb_number"] = "39488811921651"
    general = [section("dataset_overview", ("status_category",))]
    report, _ = via_api("39488811921651- this is the AWB for an order, share the status of it", records, general)
    assert report.records_analyzed == 1
    assert report.tables[0].values == [["cancelled", 1]]


def test_bug5_unknown_order_is_a_plain_message_not_an_all_shipment_report():
    with pytest.raises(NoMatchingRecordsError) as error:
        run("Order ID: 111222333 status?", mixed_week(), STATUS_SECTIONS)
    assert error.value.details["reason"] == "order_not_found"
    assert error.value.message.startswith("We couldn't find order 111222333 among your shipments between 21 Oct 2026 "
                                          "and 27 Oct 2026.")


@pytest.mark.parametrize("text, expected", [
    ("Order ID: 296803827 status?", [("order_reference", ("296803827",))]),
    ("orders 296803827, 296803828", [("order_reference", ("296803827", "296803828"))]),
    ("296803827 order no ka status batao", [("order_reference", ("296803827",))]),
    ("AWB 1234567890 kahan hai", [("awb_reference", ("1234567890",))]),
    ("39488811921651- this is the AWB for an order, share the status of it", [("awb_reference", ("39488811921651",))]),
    ("is order ka status batao 296803827", [("order_reference", ("296803827",))]),
    ("orders from 2026-10-21 in pincode 400001 above 1500 rupees", []),
    ("5 orders delivered by Delhivery", []),
    ("Show orders in pincode 400001", []),
])
def test_bug5_order_reference_detection(text, expected):
    from app.analysis.interpreter import order_reference_filters
    assert [(f.field, f.value) for f in order_reference_filters(text)] == expected


def test_bug5_status_counts_reconcile_with_the_analysed_shipments():
    report, _ = run("Shipment status breakdown", mixed_week(), STATUS_SECTIONS)
    assert report.records_analyzed == 25
    assert sum(row[1] for row in report.tables[0].values) == report.records_analyzed
    chart = report.structured_report.charts[0]
    assert sum(chart.datasets[0].data) == report.records_analyzed


def test_bug5_source_count_is_the_exact_count_the_report_reads():
    from app.api.report_routes import get_report_service
    from app.main import app
    records = mixed_week()
    upstream = Upstream(records)
    cfg = replace(settings, shipment_api_base_url="https://shipments.test")
    service = ReportService(PlannedAI(), config=cfg, source=ShipmentSource(cfg, transport=httpx.MockTransport(upstream)))
    app.dependency_overrides[get_report_service] = lambda: service
    try:
        client = TestClient(app)
        response = client.post("/api/reports/source-count", json={"filters": FILTERS},
                               headers={"X-User-Id": "seller-a", "X-Shipment-Authorization": TOKEN})
        assert response.status_code == 200, response.text
        assert response.json()["exact_count"] == 25 and response.json()["is_estimate"] is False
        assert upstream.headers[0]["authorization"] == TOKEN  # the caller's own credential scopes the count
        count_query = upstream.requests[0]
        report, report_upstream = via_api("Shipment status breakdown", records, STATUS_SECTIONS)
        assert report.records_analyzed == 25
        assert report_upstream.requests[0] == count_query  # identical count call, so the numbers cannot drift
        missing_login = client.post("/api/reports/source-count", json={"filters": FILTERS},
                                    headers={"X-Shipment-Authorization": TOKEN})
        assert missing_login.status_code == 401
    finally:
        app.dependency_overrides.pop(get_report_service, None)


def test_bug5_ist_day_boundaries_are_explicit():
    from app.models.report_schema import ReportFilters
    from app.services.shipment_source import build_query
    query = build_query(ReportFilters.model_validate(FILTERS), replace(settings, shipment_api_base_url="https://x.test"))
    assert (query.from_date, query.to_date) == ("2026-10-21 00:00:00", "2026-10-28 00:00:00")  # end is exclusive


# --- BUG 6 --------------------------------------------------------------------------------------------------

def test_bug6_exact_weight_from_the_planner_is_accepted():
    records = [shipment("1", status="Delivered", weight=5), shipment("2", status="Delivered", weight=5.0),
               shipment("3", status="Delivered", weight=4.5), shipment("4", status="Cancelled", weight=5)]
    cohort = [{"field": "weight", "op": "eq", "value": "5 kg"},
              {"field": "status_category", "op": "eq", "value": "delivered"}]
    report, ai = run("delivery 5 kg se kitne shipment Lage is week pe", records,
                     [section("five_kg_deliveries", (), ("orders",))], cohort)
    assert report.records_analyzed == 2 and cards(report) == [("Total Shipments", 2)]
    assert ai.events.count("plan") == 1


def test_bug6_shipments_delivered_by_each_courier_with_missing_courier_names():
    records = ([shipment(str(i), status="Delivered", courier="Delhivery") for i in range(3)]
               + [shipment(str(10 + i), status="Delivered", courier="Blue Dart") for i in range(2)]
               + [shipment("20", status="Delivered", courier=None), shipment("21", status="Cancelled", courier="Delhivery")])
    cohort = [{"field": "status_category", "op": "eq", "value": "delivered"}]
    specs = [section("delivered_by_courier", ("courier",), ("orders",), ranking_metric="orders", charts=["bar"])]
    report, upstream = via_api("Show how many shipments were delivered by each courier this week.", records, specs, cohort)
    assert report.records_analyzed == 6
    assert report.tables[0].values == [["Delhivery", 3], ["Blue Dart", 2], ["Not available", 1]]
    assert all(h["authorization"] == TOKEN for h in upstream.headers)


def test_bug6_unusable_plans_fall_back_to_the_rule_based_report():
    from tests.test_ordered_reports import RuleFallbackAI
    records = [shipment(str(i), status="Delivered", courier="Delhivery" if i % 2 else "Blue Dart") for i in range(6)]
    ai = RuleFallbackAI([section("x", ("courier",))],
                        alter_plan=lambda p: {**p, "cohort": [{"field": "weight", "op": "like", "value": "5"}]})
    report, _ = run("Courier wise delivered shipments", records, [], ai=ai)
    assert report.records_analyzed == 6
    assert ai.events.count("plan") == 2


def test_bug6_status_named_in_the_question_is_read_even_if_the_filter_differs():
    records = [shipment("1", status="Delivered"), shipment("2", status="Pickup Scheduled")]
    upstream = Upstream(records)
    cohort = [{"field": "status_category", "op": "eq", "value": "delivered"}]
    report, upstream = via_api("Delivered shipments by courier", records, [section("delivered", ("courier",))], cohort,
                               status="Pickup Scheduled", upstream=upstream)
    assert all(r["type"] == 0 for r in upstream.requests)  # all statuses read; the question's status decides
    assert report.records_analyzed == 1


# --- Seller isolation ---------------------------------------------------------------------------------------

def test_reports_are_built_only_from_the_callers_own_source_rows():
    seller_a = [shipment("A1"), shipment("A2")]
    report, upstream = via_api("Status breakdown", seller_a, [section("statuses", ("status_category",))])
    assert report.records_analyzed == 2
    assert {h["authorization"] for h in upstream.headers} == {TOKEN}
    assert TOKEN not in json.dumps(report.model_dump(mode="json"))
