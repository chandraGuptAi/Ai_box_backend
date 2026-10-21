"""Tests for saved reports storage, PDF/document persistence, and dashboard analytics."""

import pytest
from tests.test_reports import client, fake_ai, SAMPLE_REQUEST, STRUCTURED_AI

USER_A = {"X-User-Id": "seller-a", "X-User-Name": "Seller A"}
USER_B = {"X-User-Id": "seller-b", "X-User-Name": "Seller B"}


def test_save_and_retrieve_report_in_database(client, fake_ai):
    fake_ai.response = STRUCTURED_AI
    # 1. Generate report
    gen_resp = client.post("/api/reports/generate", json=SAMPLE_REQUEST)
    assert gen_resp.status_code == 200
    report = gen_resp.json()
    report_id = report["report_id"]

    # 2. Save report to DB with custom name and description
    save_resp = client.post("/api/reports/save", headers=USER_A, json={
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
    get_resp = client.get(f"/api/reports/saved/{report_id}", headers=USER_A)
    assert get_resp.status_code == 200
    retrieved = get_resp.json()
    assert retrieved["id"] == report_id
    assert retrieved["name"] == "January Shipment Performance Report"
    assert retrieved["report"]["report_id"] == report_id

    # 4. List saved reports
    list_resp = client.get("/api/reports/saved", headers=USER_A)
    assert list_resp.status_code == 200
    list_data = list_resp.json()
    assert list_data["total"] >= 1
    assert any(item["id"] == report_id for item in list_data["items"])

    # 5. Check dashboard overview analytics
    overview_resp = client.get("/api/reports/overview", headers=USER_A)
    assert overview_resp.status_code == 200
    overview = overview_resp.json()
    assert overview["totalReports"] >= 1
    assert overview["reportsThisMonth"] >= 1
    assert overview["lastReport"]["name"] == "January Shipment Performance Report"
    assert len(overview["recentReports"]) >= 1

    # 6. Check AI Insights overview endpoint compatibility
    insights_overview = client.get("/api/ai-insights/overview", headers=USER_A).json()
    assert insights_overview["totalReports"] >= 1
    assert insights_overview["source"] == "live"

    # 7. Download PDF from stored file
    pdf_resp = client.get(f"/api/reports/saved/{report_id}/download?format=pdf", headers=USER_A)
    assert pdf_resp.status_code == 200
    assert pdf_resp.headers["content-type"] == "application/pdf"
    assert pdf_resp.content.startswith(b"%PDF")

    # 8. Rename report
    rename_resp = client.patch(f"/api/reports/saved/{report_id}", headers=USER_A, json={
        "name": "Updated Shipment Report 2025",
        "description": "Updated note"
    })
    assert rename_resp.status_code == 200
    assert rename_resp.json()["name"] == "Updated Shipment Report 2025"

    # 9. Delete report
    del_resp = client.delete(f"/api/reports/saved/{report_id}", headers=USER_A)
    assert del_resp.status_code == 200
    assert client.get(f"/api/reports/saved/{report_id}", headers=USER_A).status_code == 404


def test_saved_reports_are_isolated_per_user(client, fake_ai):
    fake_ai.response = STRUCTURED_AI
    report = client.post("/api/reports/generate", json=SAMPLE_REQUEST).json()
    report_id = report["report_id"]
    saved = client.post("/api/reports/save", headers=USER_A,
                        json={"report_id": report_id, "name": "Seller A private report", "report": report})
    assert saved.status_code == 200
    assert saved.json()["saved_report"]["created_by_id"] == "seller-a"

    # Seller B cannot list, read, download, rename, delete or overwrite Seller A's report.
    listed = client.get("/api/reports/saved", headers=USER_B).json()
    assert all(item["id"] != report_id for item in listed["items"])
    insights = client.get("/api/ai-insights/reports", headers=USER_B).json()
    assert all(item["id"] != report_id for item in insights["items"])
    assert client.get(f"/api/reports/saved/{report_id}", headers=USER_B).status_code == 404
    assert client.get(f"/api/ai-insights/reports/{report_id}", headers=USER_B).status_code == 404
    assert client.get(f"/api/reports/saved/{report_id}/download?format=pdf", headers=USER_B).status_code == 404
    assert client.get(f"/api/ai-insights/reports/{report_id}/export", headers=USER_B).status_code == 404
    assert client.get(f"/api/reports/{report_id}", headers=USER_B).status_code == 404
    assert client.patch(f"/api/reports/saved/{report_id}", headers=USER_B, json={"name": "hijacked"}).status_code == 404
    assert client.delete(f"/api/reports/saved/{report_id}", headers=USER_B).status_code == 404
    overwrite = client.post("/api/reports/save", headers=USER_B,
                            json={"report_id": report_id, "name": "hijacked", "report": report})
    assert overwrite.status_code == 409
    overview_b = client.get("/api/reports/overview", headers=USER_B).json()
    assert all(r["id"] != report_id for r in overview_b["recentReports"])

    # Seller A still has the original, untouched.
    mine = client.get(f"/api/reports/saved/{report_id}", headers=USER_A).json()
    assert mine["name"] == "Seller A private report"
    assert client.delete(f"/api/reports/saved/{report_id}", headers=USER_A).status_code == 200


def test_saved_report_endpoints_require_user(client):
    assert client.get("/api/reports/saved").status_code == 401
    assert client.get("/api/reports/overview").status_code == 401
    assert client.get("/api/ai-insights/reports").status_code == 401
    assert client.get("/api/reports/saved", headers={"X-User-Id": "   "}).status_code == 401


def test_missing_user_is_rejected_unless_dev_user_is_configured(client, monkeypatch):
    from dataclasses import replace
    from app.api import auth

    monkeypatch.setattr(auth, "settings", replace(auth.settings, environment="development", dev_user_id=""))
    assert client.get("/api/reports/saved").status_code == 401

    monkeypatch.setattr(auth, "settings", replace(auth.settings, dev_user_id="local-dev"))
    assert client.get("/api/reports/saved").status_code == 200
    # A real header always wins over the dev user.
    assert client.get("/api/ai-insights/reports", headers=USER_A).json()["creators"][0]["id"] == "seller-a"


def test_insights_overview_without_user_is_empty(client):
    overview = client.get("/api/ai-insights/overview").json()
    assert overview["totalReports"] == 0 and overview["recentReports"] == []
