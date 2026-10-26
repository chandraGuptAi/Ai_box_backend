"""Read-only access to the report generation audit trail for the admin audit API.

Source files (written by report_activity / report_service, rotated by RotatingFileHandler):
- output/report_generation_audit.jsonl[.N]  one line per report request (user, prompt, outcome, timing)
- output/report_coverage_audit.jsonl[.N]    shipment counts per report, joined by request_id

Parsed files are cached by (size, mtime), so repeated calls do not re-read unchanged files.
"""
from __future__ import annotations

import json
import logging
import statistics
import threading
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from app.config import BASE_DIR

logger = logging.getLogger("ai_box.audit_trail")

_cache: dict[Path, tuple[tuple[int, float], list[dict[str, Any]]]] = {}
_lock = threading.Lock()


def _output_dir() -> Path:
    return BASE_DIR / "output"


def _read(name: str) -> list[dict[str, Any]]:
    """Every JSON line of ``name`` and its rotated copies (oldest first)."""
    records: list[dict[str, Any]] = []
    base = _output_dir() / name
    paths = sorted(base.parent.glob(name + ".*"), key=lambda p: -int(p.suffix[1:]) if p.suffix[1:].isdigit() else 0)
    for path in [*(p for p in paths if p.suffix[1:].isdigit()), base]:
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        key = (stat.st_size, stat.st_mtime)
        with _lock:
            cached = _cache.get(path)
        if cached and cached[0] == key:
            records.extend(cached[1])
            continue
        parsed = []
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    parsed.append(item)
        with _lock:
            _cache[path] = (key, parsed)
        records.extend(parsed)
    return records


def _coverage_by_request() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in _read("report_coverage_audit.jsonl"):
        request_id = record.get("request_id")
        if request_id and request_id not in result:
            result[request_id] = record
    return result


def _item(record: dict[str, Any], coverage: dict[str, Any] | None) -> dict[str, Any]:
    filters = record.get("filters") or {}
    completeness = (coverage or {}).get("completeness") or {}
    started = str(record.get("started_at_ist") or "")
    return {
        "request_id": record.get("request_id"),
        "report_id": record.get("report_id"),
        "date_ist": started[:10],
        "time_ist": started[11:19],
        "started_at": record.get("started_at"),
        "completed_at": record.get("completed_at"),
        "user_id": record.get("user_id"),
        "customer_id": record.get("customer_id"),
        "company_name": record.get("company_name"),
        "outcome": record.get("outcome"),
        "error_code": record.get("error_code"),
        "prompt": record.get("prompt"),
        "report_type": record.get("report_type"),
        "source": record.get("source"),
        "records_analyzed": record.get("records_analyzed"),
        "shipments_matched": completeness.get("expected_records"),
        "duration_ms": record.get("duration_ms"),
        "from_date": filters.get("from_date"),
        "to_date": filters.get("to_date"),
        "status_filter": filters.get("status"),
        "is_follow_up": record.get("is_follow_up"),
        "previous_report_id": record.get("previous_report_id"),
        "ai_calls": (record.get("generation") or {}).get("ai_calls"),
    }


def query_report_audit(*, from_date: date, to_date: date, user_id: str | None = None, outcome: str | None = None,
                       error_code: str | None = None, search: str | None = None) -> list[dict[str, Any]]:
    """Matching report requests, newest first. Dates are IST calendar days, inclusive."""
    start, end = from_date.isoformat(), to_date.isoformat()
    needle = (search or "").strip().casefold()
    coverage = _coverage_by_request()
    items = []
    for record in _read("report_generation_audit.jsonl"):
        if record.get("event") not in (None, "report_generation"):
            continue
        day = str(record.get("started_at_ist") or "")[:10]
        if not (start <= day <= end):
            continue
        if user_id and str(record.get("user_id")) != user_id:
            continue
        if outcome and record.get("outcome") != outcome:
            continue
        if error_code and record.get("error_code") != error_code:
            continue
        if needle and needle not in " ".join(str(record.get(k) or "") for k in
                                             ("prompt", "company_name", "user_id", "customer_id")).casefold():
            continue
        items.append(_item(record, coverage.get(record.get("request_id"))))
    items.sort(key=lambda i: str(i.get("started_at") or ""), reverse=True)
    return items


def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals for the filtered requests, overall and per IST day."""
    def block(rows: list[dict[str, Any]]) -> dict[str, Any]:
        generated = [r for r in rows if r.get("outcome") == "generated"]
        durations = [r["duration_ms"] for r in generated if isinstance(r.get("duration_ms"), (int, float))]
        return {
            "requests": len(rows),
            "generated": len(generated),
            "failed": len(rows) - len(generated),
            "success_rate": round(100 * len(generated) / len(rows), 1) if rows else None,
            "unique_users": len({r.get("user_id") for r in rows if r.get("user_id")}),
            "median_duration_ms": round(statistics.median(durations)) if durations else None,
            "max_duration_ms": max(durations) if durations else None,
            "errors": dict(Counter(r["error_code"] for r in rows if r.get("error_code")).most_common()),
        }

    days: dict[str, list[dict[str, Any]]] = {}
    for row in items:
        days.setdefault(row["date_ist"], []).append(row)
    users: dict[str, list[dict[str, Any]]] = {}
    for row in items:
        users.setdefault(str(row.get("user_id")), []).append(row)
    return {
        **block(items),
        "by_day": [{"date": day, **block(rows)} for day, rows in sorted(days.items())],
        "top_users": [
            {"user_id": user, "company_name": rows[0].get("company_name"), "requests": len(rows),
             "generated": sum(r.get("outcome") == "generated" for r in rows)}
            for user, rows in sorted(users.items(), key=lambda kv: -len(kv[1]))[:10]
        ],
    }


CSV_COLUMNS = ("date_ist", "time_ist", "user_id", "customer_id", "company_name", "outcome", "error_code",
               "records_analyzed", "shipments_matched", "duration_ms", "from_date", "to_date", "status_filter",
               "is_follow_up", "ai_calls", "prompt", "report_id", "request_id")
