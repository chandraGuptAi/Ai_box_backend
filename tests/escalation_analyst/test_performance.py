"""Original Node golden values, reference XLSX cells, attribution and request lifecycle."""

import io
import json
import time
from pathlib import Path
from uuid import uuid4
from openpyxl import load_workbook
import pytest
from app.escalation_analyst.performance.engine import build_performance, SHEETS
from app.escalation_analyst.performance.workbook import export_workbook
from app.escalation_analyst.performance.mappings import validate_mappings
from app.escalation_analyst.errors import EscalationError
from .test_core import client, ingest, message

FIXTURE = json.loads((Path(__file__).parent / "fixtures/performance-golden.json").read_text(encoding="utf-8"))


def test_all_ten_original_node_sheet_values_and_columns():
    f = FIXTURE
    result = build_performance(f["messages"], f["request"], f["mappings"])
    assert result["summary"] == f["result"]["summary"]
    assert list(result["sheets"]) == SHEETS
    for name, sheet in result["sheets"].items():
        assert sheet["columns"] == f["result"]["sheets"][name]["columns"], name
        assert sheet["rows"] == f["result"]["sheets"][name]["rows"], name
        assert all(len(row) == len(sheet["columns"]) for row in sheet["rows"])


def test_original_node_preview_metrics_and_distributions():
    from app.escalation_analyst.performance.details import build_details

    fixture = FIXTURE
    actual = build_details(fixture["result"], {**fixture["request"], "mappings": fixture["mappings"]})
    for key in ("kpis", "tat", "trend", "status", "classification", "kam", "seller"):
        assert actual[key] == fixture["details"][key], key


def test_workbook_preserves_positions_formulas_cached_values_and_safe_text():
    f = FIXTURE
    result = build_performance(f["messages"], f["request"], f["mappings"])
    result["sheets"]["Backup"]["rows"][0][1] = '=HYPERLINK("evil")'
    content = export_workbook(result, {"request": f["request"], "created_at": f["request"]["from"]})
    formulas = load_workbook(io.BytesIO(content), data_only=False)
    values = load_workbook(io.BytesIO(content), data_only=True)
    assert formulas.sheetnames == SHEETS
    assert formulas["Backup"]["B5"].value.startswith("'")
    assert formulas["Backup"]["F5"].data_type == "s"
    assert formulas["Backup"]["X5"].value == "=LEN(F5)"
    assert formulas["Backup"].freeze_panes == "A5"
    kam = formulas["KAM_MTD_Performance"]
    assert kam["E2"].value == "Oct" and kam["E3"].value == 2 and kam["E4"].value == 29
    assert kam.freeze_panes == "A8"
    assert kam["M8"].value.startswith("=RANK(")
    assert values["KAM_MTD_Performance"]["L8"].value == result["sheets"]["KAM_MTD_Performance"]["rows"][0][11]


def test_unknown_statuses_and_mapping_references_are_rejected():
    for mappings in [
        dict(members=[dict(senderId="a", employeeId="missing")]),
        dict(groups=[dict(groupId="a")]),
        dict(employees=[dict(id="E", name="A"), dict(id="E", name="B")]),
        dict(activity=[dict(groupId="a", date="2026-02-31", active=True)]),
    ]:
        with pytest.raises(EscalationError):
            validate_mappings(mappings)


def wait_job(client, base: str, job_id: int) -> dict:
    for _ in range(200):
        response = client.get(f"{base}/{job_id}")
        assert response.status_code == 200, response.text
        result = response.json()
        if result["status"] != "running":
            assert result["status"] == "complete", result
            return result
        time.sleep(0.01)
    raise AssertionError("Job failed to finish")


def test_logistics_and_performance_end_to_end(client):
    ingest(client)
    date = client.app.state.runtime.groups.list()[0]["last_message_ts"]
    from app.escalation_analyst.utils.ist import day

    period = day(date)
    response = client.post(
        "/logistics-reports/generate", json=dict(fromDate=period, toDate=period, requestKey=str(uuid4()))
    )
    assert response.status_code == 202, response.text
    lid = response.json()["id"]
    wait_job(client, "/logistics-reports", lid)
    assert client.get(f"/logistics-reports/{lid}/records").json()["total"] == 1
    assert client.get(f"/logistics-reports/{lid}/records/1").json()["sourceTotal"] == 1
    assert client.get(f"/logistics-reports/{lid}/export").content[:2] == b"PK"
    config = dict(period="custom", fromDate=period, toDate=period, requestKey=str(uuid4()))
    response = client.post("/performance-reports/generate", json=config)
    assert response.status_code == 200, response.text
    pid = response.json()["id"]
    wait_job(client, "/performance-reports", pid)
    assert client.post("/performance-reports/generate", json=config).json()["reused"] is True
    assert client.get(f"/performance-reports/{pid}/table?sheet=Backup").json()["total"] == 1
    assert client.get(f"/performance-reports/{pid}/details").json()["kpis"]["conversations"] == 1
    assert client.get(f"/performance-reports/{pid}/conversations").json()["total"] == 1
    assert client.get(f"/performance-reports/{pid}/timeline?groupId=test@g.us&awb=12345678").json()["events"]
    assert client.get(f"/performance-reports/{pid}/export?sheet=Backup").content[:2] == b"PK"
    assert client.get(f"/performance-reports/{pid}/pdf").content.startswith(b"%PDF")
    assert client.get(f"/performance-reports/{pid}/table?sheet=missing").status_code == 400
