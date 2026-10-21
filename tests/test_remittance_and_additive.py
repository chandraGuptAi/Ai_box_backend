"""Tests for Paid Remittance, Pending Remittance, and Incremental / Additive prompt reports."""

import pytest
from app.prompts.registry import get_report_definition
from app.prompts.remittance_prompt import (
    analyze_paid_remittance,
    analyze_pending_remittance,
    PAID_REMITTANCE_REPORT,
    PENDING_REMITTANCE_REPORT,
)
from tests.test_reports import client, fake_ai, FakeAI


SAMPLE_PAID_REMITTANCE_DATA = {
    "paidRemittanceList": [
        {
            "awbNo": "AWB1001",
            "remittanceDate": "2026-09-15",
            "deliveryeDate": "2026-09-10",
            "utrReference": "UTR99887766",
            "codDeductionAmount": "45.00",
            "remittedAmount": "955.00",
            "transactionStatus": "Paid",
            "collectableValue": "1000.00",
            "earlyCod": "No",
        },
        {
            "awbNo": "AWB1002",
            "remittanceDate": "2026-09-16",
            "deliveryeDate": "2026-09-11",
            "utrReference": "UTR99887767",
            "codDeductionAmount": "60.00",
            "remittedAmount": "1440.00",
            "transactionStatus": "Early COD Paid",
            "collectableValue": "1500.00",
            "earlyCod": "Yes",
        },
    ]
}

SAMPLE_PENDING_REMITTANCE_DATA = {
    "pendingRemittanceList": [
        {
            "awbNo": "AWB2001",
            "remittanceDueDate": "2026-10-20",
            "deliveryeDate": "2026-10-12",
            "codDeductionAmount": "50.00",
            "remittedAmount": "750.00",
            "transactionStatus": "Pending",
            "collectableValue": "800.00",
        },
        {
            "awbNo": "AWB2002",
            "remittanceDueDate": "2026-10-18",
            "deliveryeDate": "2026-10-10",
            "codDeductionAmount": "70.00",
            "remittedAmount": "1200.00",
            "transactionStatus": "On Hold",
            "collectableValue": "1270.00",
        },
    ]
}


def test_registry_resolves_remittance_reports():
    paid = get_report_definition("paid_remittance")
    assert paid.report_type == "paid_remittance"
    assert get_report_definition("paid-remittance").report_type == "paid_remittance"

    pending = get_report_definition("pending_remittance")
    assert pending.report_type == "pending_remittance"
    assert get_report_definition("pending-remittance").report_type == "pending_remittance"


def test_analyze_paid_remittance_calculates_metrics():
    analysis = analyze_paid_remittance(SAMPLE_PAID_REMITTANCE_DATA)
    assert analysis.records_analyzed == 2
    assert analysis.metrics["total_records"] == 2
    assert analysis.metrics["early_cod_transactions"] == 1
    assert "₹2,395.00" in analysis.metrics["total_remitted_amount"]
    assert len(analysis.tables) >= 1
    assert analysis.tables[0].title == "Remittance Transaction Status"


def test_analyze_pending_remittance_calculates_metrics():
    analysis = analyze_pending_remittance(SAMPLE_PENDING_REMITTANCE_DATA)
    assert analysis.records_analyzed == 2
    assert analysis.metrics["total_records"] == 2
    assert analysis.metrics["on_hold_records"] == 1
    assert "₹1,950.00" in analysis.metrics["total_pending_amount"]
    assert len(analysis.tables) >= 1
    assert analysis.tables[0].title == "Pending Remittance Status Overview"


def test_api_generates_paid_remittance_report(client, fake_ai):
    fake_ai.response = {
        "title": "Paid Remittance Analysis",
        "executive_summary": {"heading": "Summary", "paragraphs": ["2 remittance payouts were processed."]},
        "findings": [{"title": "Early COD usage", "detail": "1 transaction used Early COD.", "severity": "info"}],
        "recommendations": [{"action": "Review deduction reconciliation", "rationale": "Valid deductions.", "priority": "low"}],
        "sections": [
            {
                "id": "status_overview",
                "title": "Remittance Status Breakdown",
                "order": 1,
                "blocks": [{"type": "paragraph", "text": "All payments cleared successfully."}],
            }
        ],
    }

    response = client.post(
        "/api/reports/generate",
        json={
            "report_type": "paid_remittance",
            "prompt": "Analyze paid remittances and deductions",
            "data": SAMPLE_PAID_REMITTANCE_DATA,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["report_type"] == "paid_remittance"
    assert data["structured_report"]["title"] == "Paid Remittance Analysis"
    assert len(data["key_metrics"]) >= 2


def test_api_incremental_prompt_merges_without_losing_previous_sections(client, fake_ai):
    # Initial report
    initial_response = {
        "title": "Initial Delivery Report",
        "executive_summary": {"heading": "Summary", "paragraphs": ["Initial analysis of delivery rate."]},
        "findings": [{"title": "Good delivery", "detail": "Deliveries on track.", "severity": "info"}],
        "recommendations": [{"action": "Maintain operations", "rationale": "Smooth flow.", "priority": "low"}],
        "sections": [
            {
                "id": "delivery_sec",
                "title": "Delivery Overview",
                "order": 1,
                "blocks": [{"type": "paragraph", "text": "Initial section content."}],
            }
        ],
    }
    fake_ai.response = initial_response

    resp1 = client.post(
        "/api/reports/generate",
        json={
            "report_type": "paid_remittance",
            "prompt": "Analyze overall remittance status",
            "data": SAMPLE_PAID_REMITTANCE_DATA,
        },
    )
    assert resp1.status_code == 200
    report1 = resp1.json()
    assert len(report1["structured_report"]["sections"]) >= 1

    # Now simulate user typing a follow-up prompt:
    # "Also compare deductions and add a breakdown table"
    fake_ai.response = {
        "title": "Deduction Analysis",
        "executive_summary": {"heading": "Summary", "paragraphs": ["Deductions are within standard rates."]},
        "findings": [{"title": "Deductions Normal", "detail": "No unexpected penalties.", "severity": "info"}],
        "recommendations": [{"action": "Audit COD charges weekly", "rationale": "Keeps fees verified.", "priority": "medium"}],
        "sections": [
            {
                "id": "deduction_sec",
                "title": "Deduction Breakdown",
                "order": 1,
                "blocks": [{"type": "paragraph", "text": "Newly added section content for deductions."}],
            }
        ],
    }

    resp2 = client.post(
        "/api/reports/generate",
        json={
            "report_type": "paid_remittance",
            "prompt": "Also compare deductions and add a breakdown table",
            "data": SAMPLE_PAID_REMITTANCE_DATA,
            "previous_report": report1,
        },
    )
    assert resp2.status_code == 200, resp2.text
    report2 = resp2.json()

    # Verify that BOTH the initial section AND the newly added section exist!
    sections = report2["structured_report"]["sections"]
    section_titles = [s["title"] for s in sections]
    assert "Delivery Overview" in section_titles
    assert "Deduction Breakdown" in section_titles
    assert len(sections) >= 2

    # Verify newly added section is stamped with source_prompt
    new_sec = next(s for s in sections if s["title"] == "Deduction Breakdown")
    assert new_sec.get("source_prompt") == "Also compare deductions and add a breakdown table"


def test_chart_removal_followup(client, fake_ai):
    fake_ai.response = {
        "title": "Shipment Overview",
        "executive_summary": {"heading": "Overview", "paragraphs": ["Initial analysis."]},
        "findings": [],
        "recommendations": [],
        "sections": [],
    }
    shipment_data = {
        "getOrderList": [
            {"order_id": "1", "order_status": "Delivered", "courier_name": "Delhivery", "order_date": "2025-01-01"},
            {"order_id": "2", "order_status": "In Transit", "courier_name": "BlueDart", "order_date": "2025-01-02"},
            {"order_id": "3", "order_status": "RTO", "courier_name": "Delhivery", "order_date": "2025-01-03"},
        ]
    }
    # Initial report with 2 charts: 1 area chart, 1 donut chart
    initial_prompt = (
        "Analyse my shipment data and generate a detailed performance report. "
        "Please include exactly two charts: "
        "1. A area Chart comparing courier-wise total shipment volume and delivery counts. "
        "2. A Pie/Donut Chart showing the overall delivery status breakdown."
    )
    resp1 = client.post(
        "/api/reports/generate",
        json={"report_type": "shipment", "prompt": initial_prompt, "data": shipment_data},
    )
    assert resp1.status_code == 200
    report1 = resp1.json()
    charts1 = report1["structured_report"]["charts"]
    assert len(charts1) == 2
    types1 = [c["type"] for c in charts1]
    assert "area" in types1
    assert any(t in ("doughnut", "pie") for t in types1)

    # Follow-up 1: "remove area chart"
    resp2 = client.post(
        "/api/reports/generate",
        json={
            "report_type": "shipment",
            "prompt": "remove area chart",
            "data": shipment_data,
            "previous_report": report1,
        },
    )
    assert resp2.status_code == 200
    report2 = resp2.json()
    charts2 = report2["structured_report"]["charts"]
    assert len(charts2) == 1
    assert charts2[0]["type"] != "area"

    # Follow-up 2: "remove the chart"
    resp3 = client.post(
        "/api/reports/generate",
        json={
            "report_type": "shipment",
            "prompt": "remove the chart",
            "data": shipment_data,
            "previous_report": report2,
        },
    )
    assert resp3.status_code == 200
    report3 = resp3.json()
    charts3 = report3["structured_report"]["charts"]
    assert len(charts3) == 0

    # Follow-up 3: Hindi "chart hata do" on a two-chart report names no single chart: ask, never remove both.
    resp4 = client.post(
        "/api/reports/generate",
        json={
            "report_type": "shipment",
            "prompt": "chart hata do",
            "data": shipment_data,
            "previous_report": report1,
        },
    )
    assert resp4.status_code == 422
    assert resp4.json()["error"]["code"] == "chart_target_ambiguous"
    assert len(resp4.json()["error"]["details"]["charts"]) == 2

    # "Both charts" is explicit.
    resp4b = client.post(
        "/api/reports/generate",
        json={
            "report_type": "shipment",
            "prompt": "dono chart hata do",
            "data": shipment_data,
            "previous_report": report1,
        },
    )
    assert resp4b.status_code == 200
    assert len(resp4b.json()["structured_report"]["charts"]) == 0

    # Follow-up 4: "Include delivery status breakdown remove the chart from report"
    resp5 = client.post(
        "/api/reports/generate",
        json={
            "report_type": "shipment",
            "prompt": "Include  delivery status breakdown remove the chart from report",
            "data": shipment_data,
            "previous_report": report1,
        },
    )
    # Mixed request with an ambiguous chart target: nothing is changed, the user is asked which chart.
    assert resp5.status_code == 422
    assert resp5.json()["error"]["code"] == "chart_target_ambiguous"
