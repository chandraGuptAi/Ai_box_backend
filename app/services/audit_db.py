"""PostgreSQL copy of the report audit trail (schema ``audit``), written alongside the JSONL files.

- report_generation_audit  <- report_activity.write_report_activity (event "report_generation")
- report_coverage_audit    <- report_service.write_coverage_audit (event "report_coverage")
- report_validation_audit  <- report_service._log_validation_event (report_numeric_* / report_schema_*)

submit() never blocks or raises: a daemon thread inserts queued events in batches through a small
connection pool with bounded retries. Events that cannot be written (database down, queue full,
process exit) are appended to output/audit_db_pending.jsonl and replayed later; rows PostgreSQL rejects
as invalid data go to output/audit_db_rejected.jsonl. Nothing is dropped silently.

event_id is a UUIDv5 of the table and the event's canonical JSON (the same content as its JSONL line),
so retries, replays and scripts/migrate_audit_jsonl_to_postgres.py never store one event twice.
request_id is not used as identity: one request produces several events.
"""
from __future__ import annotations

import atexit
import json
import logging
import math
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import psycopg
from psycopg.types.json import Jsonb

from app.config import BASE_DIR

logger = logging.getLogger("ai_box.audit_db")

# Fixed forever: changing it changes every event_id and breaks duplicate detection.
_NAMESPACE = uuid.UUID("5f1d3c2e-8a4b-4c6d-9e0f-a1b2c3d4e5f6")

PENDING_FILE = "audit_db_pending.jsonl"
REJECTED_FILE = "audit_db_rejected.jsonl"

TABLES = {
    "generation": ("report_generation_audit",
                   ("request_id", "report_id", "user_id", "seller_id", "customer_id", "company_name", "prompt",
                    "report_type", "source", "outcome", "error_code", "duration_ms", "started_at", "completed_at",
                    "filters")),
    "coverage": ("report_coverage_audit",
                 ("request_id", "report_id", "user_id", "source_count", "included_count", "excluded_count",
                  "coverage_percentage")),
    "validation": ("report_validation_audit",
                   ("request_id", "report_id", "validation_type", "validation_status", "error_count",
                    "warning_count")),
}

_STOP = object()


def normalize(event: dict[str, Any]) -> dict[str, Any]:
    """The event exactly as its JSONL line stores it."""
    return json.loads(json.dumps(event, ensure_ascii=True, default=str))


def event_id(kind: str, event: dict[str, Any]) -> uuid.UUID:
    canonical = json.dumps(event, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return uuid.uuid5(_NAMESPACE, TABLES[kind][0] + "\n" + canonical)


def _clean(value: Any) -> Any:
    """JSON PostgreSQL accepts: no NUL characters, no NaN/Infinity."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {_clean(str(k)): _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _text(value: Any, limit: int | None = None) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", "")
    return text[:limit] if limit else text


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and abs(value) < 2 ** 63 else None


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _generation(e: dict[str, Any]) -> tuple[tuple, datetime | None]:
    started = _time(e.get("started_at"))
    filters = e.get("filters")
    return ((_text(e.get("request_id"), 100), _text(e.get("report_id"), 100), _text(e.get("user_id"), 150),
             _text(e.get("seller_id"), 150), _text(e.get("customer_id"), 150), _text(e.get("company_name")),
             _text(e.get("prompt")), _text(e.get("report_type"), 100), _text(e.get("source"), 100),
             _text(e.get("outcome"), 100), _text(e.get("error_code"), 150), _int(e.get("duration_ms")), started,
             _time(e.get("completed_at")), Jsonb(_clean(filters)) if isinstance(filters, dict) else None), started)


def _coverage(e: dict[str, Any]) -> tuple[tuple, datetime | None]:
    completeness = e.get("completeness") if isinstance(e.get("completeness"), dict) else {}
    details = e.get("details") if isinstance(e.get("details"), dict) else {}
    source = _int(completeness.get("expected_records"))
    if source is None:
        source = _int(details.get("expected_records"))
    included = _int(completeness.get("analyzed_records"))
    unique = _int(completeness.get("unique_records"))
    excluded = unique - included if unique is not None and included is not None else None
    percentage = (min(round(100 * included / source, 2), 99999.99)
                  if source and included is not None and source > 0 else None)
    return ((_text(e.get("request_id"), 100), _text(e.get("report_id"), 100), _text(e.get("user_id"), 150),
             source, included, excluded, percentage), _time(e.get("at")))


def _validation(e: dict[str, Any]) -> tuple[tuple, datetime | None]:
    kind = str(e.get("event") or "")
    rejected = kind.endswith("_rejection")
    status = e.get("repair_status") or ("rejected" if rejected else None)
    return ((_text(e.get("request_id"), 100), _text(e.get("report_id"), 100), _text(kind or None, 150),
             _text(status, 100), 1 if rejected else None, None), _time(e.get("at")))


_BUILDERS = {"generation": _generation, "coverage": _coverage, "validation": _validation}


def build_row(kind: str, event: dict[str, Any], fallback_time: datetime) -> tuple:
    """(event_id, *indexed columns, raw_event, created_at) for TABLES[kind]."""
    values, at = _BUILDERS[kind](event)
    return (event_id(kind, event), *values, Jsonb(_clean(event)), at or fallback_time)


def _check_schema(schema: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
        raise ValueError(f"Invalid audit schema name: {schema!r}")
    return schema


def insert_events(conn: psycopg.Connection, items: Iterable[tuple[str, dict[str, Any], datetime]],
                  schema: str = "audit") -> int:
    """Insert (kind, normalized event, fallback time) items in one transaction; returns rows newly inserted."""
    by_kind: dict[str, list[tuple]] = {}
    for kind, event, fallback in items:
        by_kind.setdefault(kind, []).append(build_row(kind, event, fallback))
    inserted = 0
    with conn.transaction(), conn.cursor() as cur:
        for kind, rows in by_kind.items():
            table, columns = TABLES[kind]
            names = ("event_id", *columns, "raw_event", "created_at")
            cur.executemany(
                f"INSERT INTO {_check_schema(schema)}.{table} ({', '.join(names)}) "
                f"VALUES ({', '.join(['%s'] * len(names))}) ON CONFLICT (event_id) DO NOTHING", rows)
            inserted += max(cur.rowcount, 0)
    return inserted


_TRANSIENT = (psycopg.OperationalError, psycopg.InterfaceError)
_BAD_DATA = (psycopg.DataError, psycopg.IntegrityError)


class AuditDbWriter:
    def __init__(self, url: str, spool_dir: Path, *, schema: str = "audit",
                 insert: Callable[[list], int] | None = None, max_queue: int = 10_000, batch_size: int = 200,
                 retry_delays: tuple[float, ...] = (0.5, 1.0, 2.0), replay_interval: float = 30.0,
                 outage_pause: float = 30.0):
        self.url, self.spool_dir, self.schema = url, Path(spool_dir), _check_schema(schema)
        self._insert = insert or self._insert_with_pool
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self.batch_size, self.retry_delays = batch_size, retry_delays
        self.replay_interval, self.outage_pause = replay_interval, outage_pause
        self._pool = None
        self._thread: threading.Thread | None = None
        self._start_lock, self._spool_lock, self._pool_lock = threading.Lock(), threading.Lock(), threading.Lock()
        self._down_until = 0.0
        self._closed = False

    # -- connections -------------------------------------------------------------------------
    @property
    def pool(self):
        with self._pool_lock:
            if self._pool is None:
                from psycopg_pool import ConnectionPool
                self._pool = ConnectionPool(
                    self.url, min_size=1, max_size=4, timeout=5, open=False, name="ai_box_audit",
                    check=ConnectionPool.check_connection,
                    kwargs={"connect_timeout": 5, "application_name": "ai_box_audit",
                            "options": "-c statement_timeout=5000"})
                self._pool.open(wait=False)
            return self._pool

    def _insert_with_pool(self, items: list) -> int:
        with self.pool.connection() as conn:
            return insert_events(conn, items, self.schema)

    # -- producer side -----------------------------------------------------------------------
    def submit(self, kind: str, event: dict[str, Any]) -> None:
        try:
            item = (kind, normalize(event), datetime.now(timezone.utc))
            if self._closed:
                self._spool([item])
                return
            self._ensure_started()
            self._queue.put_nowait(item)
        except queue.Full:
            self._spool([item])
        except Exception:  # Auditing must never break report generation.
            logger.exception("Audit event could not be queued for PostgreSQL; it remains in the JSONL file.")

    def _ensure_started(self) -> None:
        if self._thread is None:
            with self._start_lock:
                if self._thread is None:
                    self._thread = threading.Thread(target=self._run, name="audit-db-writer", daemon=True)
                    self._thread.start()

    # -- worker ------------------------------------------------------------------------------
    def _run(self) -> None:
        next_replay = 0.0
        while True:
            try:
                first = self._queue.get(timeout=1.0)
            except queue.Empty:
                first = None
            batch, stop = [], first is _STOP
            if first is not None and not stop:
                batch.append(first)
            while len(batch) < self.batch_size and not stop:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                stop = item is _STOP
                if not stop:
                    batch.append(item)
            if batch:
                self.write(batch, retries=self.retry_delays if not stop else ())
            if stop:
                return
            if time.monotonic() >= next_replay:
                next_replay = time.monotonic() + self.replay_interval
                self.replay_pending()

    def write(self, batch: list, retries: tuple[float, ...] = ()) -> bool:
        """Insert a batch; spool it if the database stays unreachable. True when it reached the database."""
        if time.monotonic() < self._down_until:
            self._spool(batch)
            return False
        for delay in (*retries, None):
            try:
                self._insert(batch)
                self._down_until = 0.0
                return True
            except _TRANSIENT as exc:
                error = exc
            except psycopg.Error as exc:
                return self._write_rows(batch, exc)
            if delay is not None and not self._closed:  # At shutdown, keep it for replay instead of waiting.
                time.sleep(delay)
            else:
                break
        logger.warning("Audit database unavailable (%s); %d event(s) kept in %s for retry.",
                       type(error).__name__, len(batch), PENDING_FILE)
        self._down_until = time.monotonic() + self.outage_pause
        self._spool(batch)
        return False

    def _write_rows(self, batch: list, batch_error: Exception) -> bool:
        """One row at a time after a non-connection error, so one bad event cannot block the others."""
        for index, item in enumerate(batch):
            try:
                self._insert([item])
            except _BAD_DATA as exc:
                logger.error("Audit event rejected by PostgreSQL (%s); kept in %s.", type(exc).__name__,
                             REJECTED_FILE)
                self._append(REJECTED_FILE, [item], reason=type(exc).__name__)
            except psycopg.Error as exc:
                logger.warning("Audit insert failed (%s / %s); %d event(s) kept in %s for retry.",
                               type(batch_error).__name__, type(exc).__name__, len(batch) - index, PENDING_FILE)
                self._spool(batch[index:])
                return False
        return True

    # -- pending file ------------------------------------------------------------------------
    def _append(self, name: str, items: list, reason: str | None = None) -> None:
        lines = "".join(json.dumps({"table": kind, "event": event, "logged_at": at.isoformat(),
                                    **({"reason": reason} if reason else {})}, ensure_ascii=True) + "\n"
                        for kind, event, at in items)
        try:
            with self._spool_lock:
                self.spool_dir.mkdir(parents=True, exist_ok=True)
                with open(self.spool_dir / name, "a", encoding="utf-8") as handle:
                    handle.write(lines)
                    handle.flush()
                    os.fsync(handle.fileno())
        except OSError:
            logger.error("Could not write %d audit event(s) to %s; they remain only in the JSONL audit files "
                         "(recoverable with scripts/migrate_audit_jsonl_to_postgres.py).", len(items), name)

    def _spool(self, items: list) -> None:
        if items:
            self._append(PENDING_FILE, items)

    def replay_pending(self) -> None:
        """Retry spooled events. Each pending file is claimed by an atomic rename, then removed once written."""
        pending = self.spool_dir / PENDING_FILE
        try:
            with self._spool_lock:
                if pending.exists() and pending.stat().st_size:
                    pending.rename(self.spool_dir / f"audit_db_pending.{os.getpid()}.{time.time_ns()}.replay")
            claimed = sorted(self.spool_dir.glob("audit_db_pending.*.replay"))
        except OSError:
            return
        for path in claimed:
            items, bad = [], []
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    record = json.loads(line)
                    items.append((record["table"], record["event"], datetime.fromisoformat(record["logged_at"])))
                except (ValueError, KeyError, TypeError):
                    bad.append(line)
            if bad:
                logger.error("%d unreadable line(s) in %s moved to %s.", len(bad), path.name, REJECTED_FILE)
                with self._spool_lock, open(self.spool_dir / REJECTED_FILE, "a", encoding="utf-8") as handle:
                    handle.writelines(json.dumps({"unparsed": line}) + "\n" for line in bad)
            self._down_until = 0.0
            for start in range(0, len(items), self.batch_size):
                chunk = items[start:start + self.batch_size]
                if not self.write(chunk):
                    # write() spooled this chunk; keep the rest of the claimed file for the next attempt.
                    rest = items[start + self.batch_size:]
                    self._spool(rest)
                    path.unlink(missing_ok=True)
                    return
            path.unlink(missing_ok=True)
            logger.info("Replayed %d pending audit event(s) to PostgreSQL.", len(items))

    # -- shutdown ----------------------------------------------------------------------------
    def close(self, timeout: float = 10.0) -> None:
        """Flush queued events (to PostgreSQL, else to the pending file) and close the pool."""
        self._closed = True
        thread = self._thread
        if thread is not None and thread.is_alive():
            try:
                self._queue.put(_STOP, timeout=1.0)
            except queue.Full:
                pass
            thread.join(timeout)
        leftover = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not _STOP:
                leftover.append(item)
        self._spool(leftover)
        if self._pool is not None:
            self._pool.close(timeout=2.0)


_writer: AuditDbWriter | None = None
_configured = False
_lock = threading.Lock()


def _enabled() -> bool:
    return os.getenv("AI_BOX_AUDIT_DB", "true").strip().lower() not in {"0", "false", "no", "off"}


def get_writer() -> AuditDbWriter | None:
    """The process writer, or None when DATABASE_URL is unset or AI_BOX_AUDIT_DB=false."""
    global _writer, _configured
    if not _configured:
        with _lock:
            if not _configured:
                url = os.getenv("DATABASE_URL", "").strip()
                if url and _enabled():
                    _writer = AuditDbWriter(url, BASE_DIR / "output",
                                            schema=os.getenv("AI_BOX_AUDIT_DB_SCHEMA", "audit").strip() or "audit")
                    atexit.register(_writer.close)
                _configured = True
    return _writer


def submit(kind: str, event: dict[str, Any]) -> None:
    """Queue one audit event for PostgreSQL. Never blocks, never raises."""
    try:
        writer = get_writer()
    except Exception:
        logger.exception("Audit database writer unavailable; event remains in the JSONL file.")
        return
    if writer is not None:
        writer.submit(kind, event)


def read_report_audit(start: datetime, end: datetime) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Generation events started in [start, end) in insertion order, and the first coverage event per request."""
    writer = get_writer()
    if writer is None:
        raise RuntimeError("Audit database is not configured")
    schema = writer.schema
    with writer.pool.connection(timeout=3) as conn:
        events = [row[0] for row in conn.execute(
            f"SELECT raw_event FROM {schema}.report_generation_audit "
            "WHERE started_at >= %s AND started_at < %s ORDER BY id", (start, end))]
        request_ids = sorted({e["request_id"] for e in events if isinstance(e.get("request_id"), str)})
        coverage = {row[0]: row[1] for row in conn.execute(
            f"SELECT DISTINCT ON (request_id) request_id, raw_event FROM {schema}.report_coverage_audit "
            "WHERE request_id = ANY(%s) ORDER BY request_id, created_at, id", (request_ids,))}
    return events, coverage
