"""Admin audit trail API: key protection, filters, summary, paging and CSV export."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import audit_trail

KEY = "test-audit-key-0123456789"


def record(n, day, *, user="s1", outcome="generated", error=None, prompt="Status breakdown", records=10):
    return {"event": "report_generation", "request_id": f"req{n}", "report_id": f"rep{n}", "user_id": user,
            "seller_id": user, "customer_id": user, "company_name": f"Company {user}", "prompt": prompt,
            "report_type": "shipment", "source": "shipment_api", "started_at": f"{day}T0{n % 10}:00:00+00:00",
            "completed_at": f"{day}T0{n % 10}:00:05+00:00", "started_at_ist": f"{day}T0{n % 10}:30:00+05:30",
            "duration_ms": 1000 * n, "outcome": outcome, "error_code": error,
            "filters": {"from_date": "2026-10-01", "to_date": day, "status": "All Statuses"},
            "is_follow_up": False, "previous_report_id": None,
            "records_analyzed": records if outcome == "generated" else None,
            "generation": {"ai_calls": 2} if outcome == "generated" else None}


@pytest.fixture
def audit(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_trail, "BASE_DIR", tmp_path)
    monkeypatch.setattr(audit_trail, "_cache", {})
    object.__setattr__(settings, "audit_api_key", KEY)
    output = tmp_path / "output"
    output.mkdir()
    rotated = [record(1, "2026-10-23", user="s1")]
    current = [record(2, "2026-10-23", user="s2", outcome="failed", error="no_matching_records", prompt="RTO docketwise"),
               record(3, "2026-10-24", user="s1", records=1780),
               record(4, "2026-10-26", user="s3")]
    (output / "report_generation_audit.jsonl.1").write_text("\n".join(map(json.dumps, rotated)) + "\n")
    (output / "report_generation_audit.jsonl").write_text("\n".join(map(json.dumps, current)) + "\nnot json\n")
    (output / "report_coverage_audit.jsonl").write_text(json.dumps(
        {"event": "report_coverage", "request_id": "req3", "completeness": {"expected_records": 1780}}) + "\n")
    yield
    object.__setattr__(settings, "audit_api_key", "")


def get(params=None, key=KEY):
    with TestClient(app) as client:
        return client.get("/api/admin/audit/reports", params=params or {},
                          headers={"X-Audit-Key": key} if key else {})


def test_requires_the_admin_key_not_the_seller_login(audit):
    assert get({"from_date": "2026-10-23"}, key=None).status_code == 401
    assert get({"from_date": "2026-10-23"}, key="wrong").status_code == 401
    with TestClient(app) as client:
        seller = client.get("/api/admin/audit/reports", headers={"X-User-Id": "s1"})
    assert seller.status_code == 401


def test_disabled_without_a_configured_key(audit):
    object.__setattr__(settings, "audit_api_key", "")
    assert get({"from_date": "2026-10-23"}).status_code == 503


def test_date_range_includes_rotated_files_newest_first_with_summary(audit):
    body = get({"from_date": "2026-10-23", "to_date": "2026-10-26"}).json()
    assert [i["request_id"] for i in body["items"]] == ["req4", "req3", "req2", "req1"]
    summary = body["summary"]
    assert (summary["requests"], summary["generated"], summary["failed"], summary["unique_users"]) == (4, 3, 1, 3)
    assert summary["errors"] == {"no_matching_records": 1}
    assert [d["date"] for d in summary["by_day"]] == ["2026-10-23", "2026-10-24", "2026-10-26"]
    assert summary["top_users"][0] == {"user_id": "s1", "company_name": "Company s1", "requests": 2, "generated": 2}
    item = next(i for i in body["items"] if i["request_id"] == "req3")
    assert item["shipments_matched"] == 1780 and item["ai_calls"] == 2 and item["time_ist"] == "03:30:00"


@pytest.mark.parametrize("params, expected", [
    ({"user_id": "s1"}, ["req3", "req1"]),
    ({"outcome": "failed"}, ["req2"]),
    ({"error_code": "no_matching_records"}, ["req2"]),
    ({"search": "docketwise"}, ["req2"]),
    ({"search": "company s3"}, ["req4"]),
])
def test_filters(audit, params, expected):
    body = get({"from_date": "2026-10-23", "to_date": "2026-10-26", **params}).json()
    assert [i["request_id"] for i in body["items"]] == expected


def test_paging(audit):
    body = get({"from_date": "2026-10-23", "to_date": "2026-10-26", "page": 2, "page_size": 3}).json()
    assert (body["total"], body["total_pages"], [i["request_id"] for i in body["items"]]) == (4, 2, ["req1"])
    assert body["summary"]["requests"] == 4  # the summary covers every matching row, not only the page


def test_csv_export(audit):
    response = get({"from_date": "2026-10-23", "to_date": "2026-10-26", "format": "csv"})
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    assert "ai_box_audit_2026-10-23_to_2026-10-26.csv" in response.headers["content-disposition"]
    lines = response.text.lstrip("﻿").strip().splitlines()
    assert lines[0].startswith("date_ist,time_ist,user_id") and len(lines) == 5


@pytest.mark.parametrize("params", [{"from_date": "2026-10-26", "to_date": "2026-10-23"},
                                    {"from_date": "2026-01-01", "to_date": "2026-10-26"},
                                    {"from_date": "23-10-2026"}, {"page_size": 501}])
def test_invalid_queries(audit, params):
    assert get(params).status_code in (400, 422)
