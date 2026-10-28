"""PostgreSQL audit trail: identity, mapping, concurrency, outages, migration and API reads.

Unit tests use an in-memory database. Tests marked ``postgres`` run only when AUDIT_TEST_DATABASE_URL is
set; they copy the audit tables' structure into a throwaway schema and never touch schema ``audit``.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from app.models.report_schema import ReportRequest
from app.services import audit_db, audit_trail, report_activity, report_service

from tests.conftest import real_write_coverage_audit

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("migrate_audit", ROOT / "scripts/migrate_audit_jsonl_to_postgres.py")
migrate_audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migrate_audit)


class FakeDb:
    """Rows by event_id, built with the real row mapping; can simulate outages and bad rows."""

    def __init__(self):
        self.rows: dict[uuid.UUID, tuple[str, tuple]] = {}
        self.down = False
        self.bad: set[str] = set()
        self.calls = 0
        self._lock = threading.Lock()

    def insert(self, items):
        with self._lock:
            self.calls += 1
            if self.down:
                raise psycopg.OperationalError("connection refused")
            if any(event.get("request_id") in self.bad for _, event, _ in items):
                raise psycopg.errors.NumericValueOutOfRange("bad value")
            new = 0
            for kind, event, fallback in items:
                row = audit_db.build_row(kind, event, fallback)
                if row[0] not in self.rows:
                    self.rows[row[0]] = (kind, row)
                    new += 1
            return new


def writer_for(tmp_path, db, **kwargs):
    options = dict(retry_delays=(0.0,), replay_interval=3600, outage_pause=0.0)
    return audit_db.AuditDbWriter("postgresql://unused", tmp_path, insert=db.insert, **{**options, **kwargs})


def pending_lines(tmp_path):
    return [line for path in tmp_path.glob("audit_db_pending*") for line in path.read_text().splitlines()]


def generation(n=1, **extra):
    return {"event": "report_generation", "request_id": f"req{n}", "report_id": f"rep{n}",
            "report_type": "shipment", "source": "shipment_api", "started_at": "2026-10-27T04:21:12.618165+00:00",
            "completed_at": "2026-10-27T04:21:18.000000+00:00", "started_at_ist": "2026-10-27T09:51:12.618165+05:30",
            "duration_ms": 5382, "outcome": "generated", "error_code": None, "allow_partial": False,
            "is_follow_up": False, "previous_report_id": None, "records_analyzed": 12, **extra}


# -- identity and mapping ----------------------------------------------------------------------

def test_event_id_is_deterministic_and_independent_of_key_order():
    event = generation()
    assert audit_db.event_id("generation", event) == audit_db.event_id("generation", dict(reversed(event.items())))
    assert audit_db.event_id("generation", event) != audit_db.event_id("generation", generation(2))
    # The same content in another table is another event; request_id alone is never the identity.
    assert audit_db.event_id("generation", event) != audit_db.event_id("coverage", event)
    assert audit_db.event_id("generation", event) != audit_db.event_id("generation", {**event, "outcome": "failed"})


def test_generation_row_mapping():
    event = audit_db.normalize(generation(seller_id="s1", user_id="s1", prompt="p", company_name="C",
                                          filters={"from_date": "2026-10-01"}, error_code="x" * 400))
    row = audit_db.build_row("generation", event, datetime(2000, 1, 1, tzinfo=timezone.utc))
    columns = dict(zip(("event_id", *audit_db.TABLES["generation"][1], "raw_event", "created_at"), row))
    assert columns["request_id"] == "req1" and columns["outcome"] == "generated" and columns["duration_ms"] == 5382
    assert columns["user_id"] == columns["seller_id"] == "s1" and columns["prompt"] == "p"
    assert len(columns["error_code"]) == 150                                  # column limit; raw_event keeps all
    assert columns["raw_event"].obj["error_code"] == "x" * 400
    assert columns["filters"].obj == {"from_date": "2026-10-01"}
    assert columns["created_at"] == columns["started_at"] == datetime.fromisoformat(event["started_at"])


def test_coverage_and_validation_row_mapping():
    fallback = datetime(2026, 10, 21, tzinfo=timezone.utc)
    coverage = {"event": "report_coverage", "at": "2026-10-21T12:10:51+00:00", "request_id": "r", "outcome": "generated",
                "completeness": {"expected_records": 251, "unique_records": 250, "analyzed_records": 200}}
    row = audit_db.build_row("coverage", coverage, fallback)
    assert row[4:8] == (251, 200, 50, 79.68) and row[-1] == datetime(2026, 10, 21, 12, 10, 51, tzinfo=timezone.utc)
    empty = audit_db.build_row("coverage", {"event": "report_coverage", "outcome": "no_matching_records",
                                            "details": {"expected_records": 0}}, fallback)
    assert empty[4:8] == (0, None, None, None) and empty[-1] == fallback
    rejection = audit_db.build_row("validation", {"event": "report_numeric_rejection", "trace_id": "t"}, fallback)
    repair = audit_db.build_row("validation", {"event": "report_schema_repair", "repair_status": "failed"}, fallback)
    assert rejection[3:7] == ("report_numeric_rejection", "rejected", 1, None)
    assert repair[3:7] == ("report_schema_repair", "failed", None, None)
    nan = audit_db.build_row("validation", {"event": "x", "value": float("nan"), "text": "a\x00b"}, fallback)
    assert nan[-2].obj == {"event": "x", "value": None, "text": "ab"}       # storable as jsonb


def test_live_event_id_matches_its_jsonl_line(tmp_path, monkeypatch):
    """The migration of a JSONL line finds the row the live writer stored for the same event."""
    db = FakeDb()
    writer = writer_for(tmp_path / "spool", db)
    monkeypatch.setattr(audit_db, "_writer", writer)
    monkeypatch.setattr(report_activity, "BASE_DIR", tmp_path)
    monkeypatch.setattr(report_service, "BASE_DIR", tmp_path)
    loggers = [logging.getLogger(n) for n in ("ai_box.report_activity", "ai_box.coverage_audit", "ai_box.validation_audit")]
    saved = [(lg, list(lg.handlers), lg.level, lg.propagate) for lg in loggers]
    for lg in loggers:
        lg.handlers = []
    try:
        report_activity.write_report_activity(generation(prompt="Status breakdown", token="never stored"))
        real_write_coverage_audit({"event": "report_coverage", "at": datetime.now(timezone.utc), "request_id": "req1",
                                   "completeness": {"expected_records": 3, "analyzed_records": 3, "unique_records": 3}})
        report_service._log_validation_event({"event": "report_numeric_rejection", "trace_id": "t1", "value": "12%"})
        writer.close()
    finally:
        for lg, handlers, level, propagate in saved:
            for handler in lg.handlers:
                handler.close()
            lg.handlers, lg.level, lg.propagate = handlers, level, propagate
    lines = {kind: (tmp_path / "output" / name).read_text().splitlines() for kind, name in migrate_audit.FILES.items()}
    expected = {audit_db.event_id(kind, audit_db.normalize(json.loads(line))) for kind, ls in lines.items() for line in ls}
    assert set(db.rows) == expected and len(expected) == 3
    stored = [row[-2].obj for kind, row in db.rows.values() if kind == "generation"]
    assert stored[0]["prompt"] == "Status breakdown" and "token" not in stored[0]   # same allow-list as the file


# -- writer behaviour ----------------------------------------------------------------------

def test_concurrent_events_are_all_stored_once(tmp_path):
    db = FakeDb()
    writer = writer_for(tmp_path, db, batch_size=50)

    def produce(t):
        for i in range(100):
            writer.submit("generation", generation(t * 1000 + i))
            writer.submit("generation", generation(t * 1000 + i))       # the same event twice

    threads = [threading.Thread(target=produce, args=(t,)) for t in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    writer.close()
    assert len(db.rows) == 1600 and not pending_lines(tmp_path)


def test_submit_returns_immediately_while_the_database_hangs(tmp_path):
    release = threading.Event()
    db = FakeDb()

    def slow_insert(items):
        release.wait(5)
        return db.insert(items)

    writer = audit_db.AuditDbWriter("postgresql://unused", tmp_path, insert=slow_insert, replay_interval=3600)
    started = datetime.now()
    for n in range(200):
        writer.submit("generation", generation(n))
    assert (datetime.now() - started).total_seconds() < 1
    release.set()
    writer.close()
    assert len(db.rows) == 200


def test_outage_keeps_events_and_replays_them_without_duplicates(tmp_path):
    db = FakeDb()
    db.down = True
    writer = writer_for(tmp_path, db)
    batch = [("generation", audit_db.normalize(generation(n)), datetime.now(timezone.utc)) for n in range(5)]
    assert writer.write(batch, retries=(0.0, 0.0)) is False
    assert db.calls == 3                                                   # bounded: first try + 2 retries
    assert len(pending_lines(tmp_path)) == 5

    writer.replay_pending()                                                # still down: nothing is lost
    assert len(pending_lines(tmp_path)) == 5 and not db.rows

    db.down = False
    writer.write(batch[:2])                                                # some arrive by another path first
    writer.replay_pending()
    assert len(db.rows) == 5 and not pending_lines(tmp_path)
    writer.replay_pending()
    assert len(db.rows) == 5


def test_full_queue_and_shutdown_spool_instead_of_dropping(tmp_path, monkeypatch):
    db = FakeDb()
    db.down = True
    writer = writer_for(tmp_path, db, max_queue=2)
    monkeypatch.setattr(writer, "_ensure_started", lambda: None)            # nothing drains the queue
    for n in range(5):
        writer.submit("generation", generation(n))
    assert len(pending_lines(tmp_path)) == 3
    writer.close()                                                         # the 2 queued events are flushed
    assert len(pending_lines(tmp_path)) == 5
    writer.submit("generation", generation(9))                             # after close: spooled too
    assert len(pending_lines(tmp_path)) == 6


def test_a_bad_row_is_kept_aside_and_does_not_block_the_batch(tmp_path):
    db = FakeDb()
    db.bad = {"req2"}
    writer = writer_for(tmp_path, db)
    batch = [("generation", audit_db.normalize(generation(n)), datetime.now(timezone.utc)) for n in range(4)]
    assert writer.write(batch) is True
    assert len(db.rows) == 3
    rejected = [json.loads(line) for line in (tmp_path / audit_db.REJECTED_FILE).read_text().splitlines()]
    assert [r["event"]["request_id"] for r in rejected] == ["req2"] and rejected[0]["reason"]


def test_submit_never_raises(monkeypatch):
    monkeypatch.setattr(audit_db, "_configured", False)
    monkeypatch.setattr(audit_db, "_enabled", lambda: 1 / 0)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    audit_db.submit("generation", generation())
    monkeypatch.setattr(audit_db, "_configured", True)
    audit_db.submit("generation", generation())                            # disabled writer: a no-op


def test_report_generation_reaches_the_audit_database(tmp_path, monkeypatch):
    db = FakeDb()
    writer = writer_for(tmp_path / "spool", db)
    monkeypatch.setattr(audit_db, "_writer", writer)
    monkeypatch.setattr(report_activity, "BASE_DIR", tmp_path)
    monkeypatch.setattr(report_service, "write_report_activity", report_activity.write_report_activity)
    audit = logging.getLogger("ai_box.report_activity")
    saved = list(audit.handlers)
    audit.handlers = []
    service = report_service.ReportService(SimpleNamespace(model="fake-model"))

    async def generate(*args, **kwargs):
        return SimpleNamespace(report_id="r1", customer_id="c1", records_analyzed=12, generation={"ai_calls": 1})

    monkeypatch.setattr(service, "_generate_report", generate)
    request = ReportRequest.model_validate({"report_type": "shipment", "data": {"getOrderList": []},
                                            "prompt": "Seller report"})
    try:
        asyncio.run(service.generate_report(request, user_id="s1"))
        writer.close()
    finally:
        for handler in audit.handlers:
            handler.close()
        audit.handlers = saved
    [(kind, row)] = db.rows.values()
    columns = dict(zip(("event_id", *audit_db.TABLES[kind][1], "raw_event", "created_at"), row))
    line = json.loads((tmp_path / "output/report_generation_audit.jsonl").read_text())
    assert kind == "generation" and columns["raw_event"].obj == line
    assert columns["outcome"] == "generated" and columns["report_id"] == "r1"
    assert columns["seller_id"] == columns["user_id"] == "s1" and columns["prompt"] == "Seller report"


# -- real PostgreSQL ---------------------------------------------------------------------------

postgres = pytest.mark.skipif(not os.getenv("AUDIT_TEST_DATABASE_URL"), reason="AUDIT_TEST_DATABASE_URL not set")


@pytest.fixture
def pg_schema():
    url = os.environ["AUDIT_TEST_DATABASE_URL"]
    schema = "audit_test_" + uuid.uuid4().hex[:12]
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        for table, _ in audit_db.TABLES.values():
            conn.execute(f"CREATE TABLE {schema}.{table} (LIKE audit.{table} INCLUDING CONSTRAINTS INCLUDING INDEXES)")
            conn.execute(f"ALTER TABLE {schema}.{table} ALTER id ADD GENERATED BY DEFAULT AS IDENTITY, "
                         "ALTER event_id SET DEFAULT gen_random_uuid(), ALTER created_at SET DEFAULT now()")
    try:
        yield url, schema
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


def count(url, schema, table):
    with psycopg.connect(url) as conn:
        return conn.execute(f"SELECT count(*), count(DISTINCT event_id) FROM {schema}.{table}").fetchone()


def write_jsonl(path, events, *, duplicate_first=False, junk=False):
    lines = [json.dumps(e) for e in events] + ([json.dumps(events[0])] if duplicate_first else [])
    path.write_text("\n".join(lines + (["not json"] if junk else [])) + "\n")


@postgres
def test_migration_is_idempotent_and_matches_live_writes(pg_schema, tmp_path):
    url, schema = pg_schema
    write_jsonl(tmp_path / "report_generation_audit.jsonl.1", [generation(1)])
    write_jsonl(tmp_path / "report_generation_audit.jsonl", [generation(2), generation(3)], duplicate_first=True, junk=True)
    write_jsonl(tmp_path / "report_coverage_audit.jsonl", [{"event": "report_coverage", "request_id": "req2",
                                                            "at": "2026-10-27T04:21:18+00:00"}])
    write_jsonl(tmp_path / "report_validation.jsonl", [{"event": "report_numeric_repair", "trace_id": "t",
                                                        "repair_status": "succeeded"}])
    # One event was already stored by the live writer before the migration ran.
    live = audit_db.AuditDbWriter(url, tmp_path / "spool", schema=schema, replay_interval=3600)
    live.submit("generation", generation(3))
    live.close()

    with psycopg.connect(url) as conn:
        first = migrate_audit.migrate(conn, tmp_path, schema)
        second = migrate_audit.migrate(conn, tmp_path, schema)
    assert first["generation"] == {"files": 2, "events": 4, "unreadable_lines": 1, "inserted": 2, "already_present": 2}
    assert first["coverage"]["inserted"] == 1 and first["validation"]["inserted"] == 1
    assert all(stats["inserted"] == 0 for stats in second.values())
    assert count(url, schema, "report_generation_audit") == (3, 3)
    with psycopg.connect(url) as conn:
        created = conn.execute(f"SELECT created_at FROM {schema}.report_validation_audit").fetchone()[0]
    assert created == datetime.fromtimestamp((tmp_path / "report_validation.jsonl").stat().st_mtime, timezone.utc)


@postgres
def test_unreachable_database_spools_then_replays_into_postgres(pg_schema, tmp_path):
    url, schema = pg_schema
    down = audit_db.AuditDbWriter("postgresql://u:p@127.0.0.1:1/none", tmp_path, schema=schema,
                                  retry_delays=(0.0,), replay_interval=3600)
    for n in range(3):
        down.submit("generation", generation(n))
    down.close(timeout=30)
    assert len(pending_lines(tmp_path)) == 3
    up = audit_db.AuditDbWriter(url, tmp_path, schema=schema, replay_interval=3600)
    up.replay_pending()
    up.replay_pending()
    up.close()
    assert count(url, schema, "report_generation_audit") == (3, 3) and not pending_lines(tmp_path)


@postgres
def test_audit_api_reads_postgres_with_the_same_response_as_jsonl(pg_schema, tmp_path, monkeypatch):
    from tests.test_audit_routes import KEY, get, record
    from app.config import settings

    url, schema = pg_schema
    output = tmp_path / "output"
    output.mkdir()
    write_jsonl(output / "report_generation_audit.jsonl.1", [record(1, "2026-10-23", user="s1")])
    write_jsonl(output / "report_generation_audit.jsonl",
                [record(2, "2026-10-23", user="s2", outcome="failed", error="no_matching_records"),
                 record(3, "2026-10-24", user="s1", records=1780), record(4, "2026-10-26", user="s3")], junk=True)
    write_jsonl(output / "report_coverage_audit.jsonl",
                [{"event": "report_coverage", "request_id": "req3", "completeness": {"expected_records": 1780}}])
    with psycopg.connect(url) as conn:
        migrate_audit.migrate(conn, output, schema)
    monkeypatch.setattr(audit_trail, "BASE_DIR", tmp_path)
    monkeypatch.setattr(audit_trail, "_cache", {})
    object.__setattr__(settings, "audit_api_key", KEY)
    writer = audit_db.AuditDbWriter(url, tmp_path / "spool", schema=schema)
    monkeypatch.setattr(audit_db, "_writer", writer)
    try:
        for params in ({"from_date": "2026-10-23", "to_date": "2026-10-26"}, {"from_date": "2026-10-24"},
                       {"from_date": "2026-10-23", "to_date": "2026-10-26", "user_id": "s1", "format": "csv"}):
            monkeypatch.setenv("AI_BOX_AUDIT_READ_SOURCE", "jsonl")
            from_files = get(params)
            monkeypatch.setenv("AI_BOX_AUDIT_READ_SOURCE", "postgres")
            from_db = get(params)
            assert from_db.status_code == from_files.status_code == 200
            assert from_db.content == from_files.content
        assert get({"from_date": "2026-10-23", "to_date": "2026-10-26"}).json()["total"] == 4
        # Database unreachable: the API still answers, from the files.
        monkeypatch.setattr(audit_db, "read_report_audit", lambda *a: (_ for _ in ()).throw(psycopg.OperationalError()))
        assert get({"from_date": "2026-10-23", "to_date": "2026-10-26"}).json()["total"] == 4
    finally:
        writer.close()
        object.__setattr__(settings, "audit_api_key", "")
