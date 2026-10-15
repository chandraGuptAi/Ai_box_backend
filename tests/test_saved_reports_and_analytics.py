"""Tests for saved reports storage, PDF/document persistence, and dashboard analytics."""

import pytest
from tests.test_reports import client, fake_ai, SAMPLE_REQUEST, STRUCTURED_AI


def test_save_and_retrieve_report_in_database(client, fake_ai):
    fake_ai.response = STRUCTURED_AI
    # 1. Generate report
    gen_resp = client.post("/api/reports/generate", json=SAMPLE_REQUEST)
    assert gen_resp.status_code == 200
    report = gen_resp.json()
    report_id = report["report_id"]

    # 2. Save report to DB with custom name and description
    save_resp = client.post("/api/reports/save", json={
        "report_id": report_id,
        "name": "January Shipment Performance Report",
        "description": "Comprehensive courier and delivery breakdown for Jan 2025",
        "report": report
    })
    assert save_resp.status_code == 200
    saved = save_resp.json()
    assert saved["success"] is True
    assert saved["saved_report"]["name"] == "January Shipment Performance Report"

    # 3. Retrieve saved report by ID
    get_resp = client.get(f"/api/reports/saved/{report_id}")
    assert get_resp.status_code == 200
    retrieved = get_resp.json()
    assert retrieved["id"] == report_id
    assert retrieved["name"] == "January Shipment Performance Report"
    assert retrieved["report"]["report_id"] == report_id

    # 4. List saved reports
    list_resp = client.get("/api/reports/saved")
    assert list_resp.status_code == 200
    list_data = list_resp.json()
    assert list_data["total"] >= 1
    assert any(item["id"] == report_id for item in list_data["items"])

    # 5. Check dashboard overview analytics
    overview_resp = client.get("/api/reports/overview")
    assert overview_resp.status_code == 200
    overview = overview_resp.json()
    assert overview["totalReports"] >= 1
    assert overview["reportsThisMonth"] >= 1
    assert overview["lastReport"]["name"] == "January Shipment Performance Report"
    assert len(overview["recentReports"]) >= 1

    # 6. Check AI Insights overview endpoint compatibility
    insights_overview = client.get("/api/ai-insights/overview").json()
    assert insights_overview["totalReports"] >= 1
    assert insights_overview["source"] == "live"

    # 7. Download PDF from stored file
    pdf_resp = client.get(f"/api/reports/saved/{report_id}/download?format=pdf")
    assert pdf_resp.status_code == 200
    assert pdf_resp.headers["content-type"] == "application/pdf"
    assert pdf_resp.content.startswith(b"%PDF")

    # 8. Rename report
    rename_resp = client.patch(f"/api/reports/saved/{report_id}", json={
        "name": "Updated Shipment Report 2025",
        "description": "Updated note"
    })
    assert rename_resp.status_code == 200
    assert rename_resp.json()["name"] == "Updated Shipment Report 2025"

    # 9. Delete report
    del_resp = client.delete(f"/api/reports/saved/{report_id}")
    assert del_resp.status_code == 200
    assert client.get(f"/api/reports/saved/{report_id}").status_code == 404
