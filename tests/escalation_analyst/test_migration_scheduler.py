"""Dry-run safety, repeatable old-data import, persistent schedules and sidecar outages."""

from dataclasses import replace
import io
import json
from pathlib import Path
import sqlite3
import time
from unittest.mock import Mock
from uuid import uuid4
import pytest
from .test_core import client, ingest, message
from .test_performance import wait_job
from app.escalation_analyst.migrate import migrate
from app.escalation_analyst.settings import Settings
from app.escalation_analyst.repositories.database import Database
from app.escalation_analyst.utils.ist import midnight, day
from app.escalation_analyst.scheduler import tick


def test_migration_dry_run_and_idempotent_import(tmp_path):
    old = tmp_path / "node.sqlite"
    new = tmp_path / "python.sqlite"
    settings = replace(Settings(), database_url="", sqlite_path=str(new))
    with sqlite3.connect(old) as conn:
        conn.executescript(
            "CREATE TABLE messages(id INTEGER PRIMARY KEY,wa_id TEXT,group_id TEXT,group_name TEXT,body TEXT,timestamp INTEGER,created_at INTEGER);CREATE TABLE group_activity_policy(id TEXT PRIMARY KEY,days INTEGER,enabled INTEGER);"
        )
        conn.execute("INSERT INTO messages VALUES(1,'wa1','g','Old group','AWB 12345678',100,100)")
        conn.execute("INSERT INTO group_activity_policy VALUES('default',14,1)")
    original = old.read_bytes()
    assert migrate(str(old), settings=settings)["tables"]["messages"] == 1
    assert not new.exists()
    assert migrate(str(old), apply=True, settings=settings)["applied"]
    migrate(str(old), apply=True, settings=settings)
    assert old.read_bytes() == original
    db = Database(settings)
    assert db.read("SELECT COUNT(*) AS n FROM messages")[0]["n"] == 1
    assert db.read("SELECT days FROM group_activity_policy")[0]["days"] == 14
    assert db.read("SELECT group_name FROM whatsapp_groups")[0]["group_name"] == "Old group"


def test_disconnected_status_keeps_saved_groups_and_protects_reconnect(client):
    ingest(client)
    r = client.app.state.runtime
    from app.escalation_analyst.errors import EscalationError

    r.sidecar.call = Mock(side_effect=EscalationError(503, "Sidecar is offline"))
    assert client.get("/whatsapp/status").json()["state"] == "disconnected"
    assert client.get("/groups").json()["groups"][0]["total_messages"] == 1
    assert client.post("/whatsapp/reconnect").status_code == 503


def test_master_progress_and_group_claim(client):
    ingest(client)
    r = client.app.state.runtime
    r.provider.generate = Mock(return_value='Analysis\n```json\n{"raised":1,"closed":0}\n```')
    response = client.post("/master/analyse", json={"hours": 24})
    assert response.status_code == 200, response.text
    job_id = response.json()["runId"]
    for _ in range(300):
        run = client.get(f"/master/progress?runId={job_id}").json()["run"]
        if run["status"] != "running":
            break
        time.sleep(0.01)
    assert run["status"] == "complete", run
    assert run["master_report_id"]
    assert client.get("/master/reports").json()["reports"]
    assert r.jobs.list("group")[0]["status"] == "complete"


def test_generation_filters_and_edited_source_freshness(client):
    ingest(client)
    period = day(time.time())
    body = dict(
        period="custom", fromDate=period, toDate=period, observed=["Delivered"], requestKey=str(uuid4())
    )
    job_id = client.post("/performance-reports/generate", json=body).json()["id"]
    wait_job(client, "/performance-reports", job_id)
    assert client.get(f"/performance-reports/{job_id}/table?sheet=Backup").json()["total"] == 0
    assert not client.get(f"/performance-reports/{job_id}/details").json()["freshness"]["stale"]
    client.app.state.runtime.groups.update_body(1, "AWB 12345678 delivered")
    assert client.get(f"/performance-reports/{job_id}/details").json()["freshness"]["stale"]
