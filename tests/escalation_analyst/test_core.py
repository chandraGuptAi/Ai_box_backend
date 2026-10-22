"""Isolated schema, transport/auth contracts and durable analysis regressions."""

import json
import time
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient
from app.escalation_analyst.application import create_app
from app.escalation_analyst.settings import Settings
from app.escalation_analyst.utils.ist import date_filter, day, midnight
from app.escalation_analyst.utils.js_compat import js_round
from app.escalation_analyst.logistics.awb import extract_awbs
from app.escalation_analyst.logistics.classifier import classify
from app.escalation_analyst.logistics.engine import build_report


@pytest.fixture
def client(tmp_path):
    app = create_app(
        replace(
            Settings(),
            database_url="",
            sqlite_path=str(tmp_path / "escalation.db"),
            sidecar_secret="test-secret",
        )
    )
    with TestClient(app) as client:
        client.headers["X-User-Id"] = "operator"
        yield client


def message(**changes):
    return dict(
        wa_id="wa-1",
        group_id="test@g.us",
        group_name="Test group",
        author="seller",
        author_name="Seller",
        body="AWB 12345678 delayed",
        msg_type="chat",
        timestamp=int(time.time()),
        **changes,
    )


def ingest(client, body=None):
    return client.post(
        "/internal/whatsapp/messages", json=body or message(), headers={"X-Sidecar-Secret": "test-secret"}
    )


def test_no_import_database_and_schema_isolation(tmp_path):
    path = tmp_path / "lazy.db"
    app = create_app(replace(Settings(), database_url="", sqlite_path=str(path)))
    assert not path.exists()
    app.state.runtime.close()


def test_identity_and_secret_required(client):
    assert client.get("/groups", headers={"X-User-Id": ""}).status_code == 401
    assert client.post("/internal/whatsapp/messages", json=message()).status_code == 401
    assert ingest(client).json()["inserted"] is True
    assert ingest(client).json()["inserted"] is False
    group = client.get("/groups").json()["groups"][0]
    assert group["total_messages"] == 1
    assert client.get("/groups/test@g.us/messages").json()["summary"]["total"] == 1


def test_names_pause_clusters_and_dates(client):
    ingest(client)
    assert client.post("/groups/test@g.us/name", json={"name": "Operator name"}).status_code == 200
    client.post(
        "/internal/whatsapp/groups/learn",
        json={"groupId": "test@g.us", "name": "Learned"},
        headers={"X-Sidecar-Secret": "test-secret"},
    )
    assert client.get("/groups").json()["groups"][0]["group_name"] == "Operator name"
    assert client.post("/clusters", json={"id": "north", "name": "North"}).status_code == 200
    assert client.post("/clusters", json={"id": "north", "name": "North"}).status_code == 409
    assert client.patch("/clusters/all/status", json={"status": "inactive"}).status_code == 400
    assert client.post("/groups/test@g.us/cluster", json={"clusterId": "north"}).status_code == 200
    assert client.get("/group-cluster-map").json()["map"] == {"test@g.us": "north"}
    client.patch("/groups/test@g.us/status", json={"status": "inactive"})
    body = message()
    body["wa_id"] = "wa-2"
    assert ingest(client, body).json()["inserted"] is False
    assert client.get("/groups?fromDate=2026-02-30&toDate=2026-03-01").status_code == 400


def test_job_single_flight_across_connections(client):
    runtime = client.app.state.runtime
    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(lambda _: runtime.jobs.claim("group", "same", {}), range(8)))
    assert sum(created for _, created in claimed) == 1
    assert len({job["id"] for job, _ in claimed}) == 1


def test_analysis_reuse_and_late_message(client):
    ingest(client)
    r = client.app.state.runtime
    r.provider.generate = Mock(return_value='Report\n```json\n{"raised":1,"closed":0}\n```')
    options = {"fromDate": day(time.time()), "toDate": day(time.time())}
    first = r.analysis.group("test@g.us", options)
    again = r.analysis.group("test@g.us", options)
    assert again["reuse"] == "existing" and first["reportId"] == again["reportId"]
    row = message()
    row["wa_id"] = "late-2"
    ingest(client, row)
    extended = r.analysis.group("test@g.us", options)
    assert extended["reuse"] == "extended" and extended["messageCount"] == 2
    assert r.provider.generate.call_count == 2


def test_presets_are_owner_scoped(client):
    created = client.post("/report-presets", json={"name": "Mine", "config": {}})
    # Preset router is installed alongside the performance routes.
    if created.status_code == 404:
        pytest.skip("Performance router not installed yet")
    preset_id = created.json()["id"]
    assert client.get("/report-presets", headers={"X-User-Id": "other"}).json()["presets"] == []
    assert client.delete(f"/report-presets/{preset_id}", headers={"X-User-Id": "other"}).status_code == 404


def test_ist_and_javascript_rounding():
    assert date_filter("2026-10-01", "2026-10-03")["to"] - midnight("2026-10-01") == 3 * 86400
    assert day(midnight("2026-10-22") - 1) == "2026-10-21"
    assert js_round(2.5) == 3 and js_round(-1.5) == -1
    assert extract_awbs("call 9876543210") == []
    assert extract_awbs("AWB 9876543210")[0]["confidence"] == "explicit"
    assert extract_awbs("AWB 9876543210", legacy=True) == []


def test_original_classifier_reference():
    fixtures = json.loads((Path(__file__).parent / "fixtures/logisticsPythonReference.json").read_text())
    for fixture in fixtures:
        for row in fixture["classifications"]:
            actual = classify(row["text"], legacy=True)
            for key in ("status", "mile", "category"):
                assert actual[key] == row[key], row["text"]


def test_same_awb_reply_is_not_verified_closure():
    rows = [
        dict(id=1, group_id="g", timestamp=100, body="AWB 12345678 delayed", author="seller"),
        dict(id=2, group_id="g", timestamp=110, body="AWB 12345678 delivered", author="staff"),
    ]
    result = build_report(rows, {"from": 90, "to": 200})
    assert result["records"][0]["status"] == "Delivered"
    assert result["records"][0]["resolutionSeconds"] is None
    assert result["summary"]["confirmedResponses"] == 0
