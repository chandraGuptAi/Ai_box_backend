"""Copy the JSONL audit history into PostgreSQL schema ``audit``. Safe to run any number of times.

    python scripts/migrate_audit_jsonl_to_postgres.py [--output-dir /app/output] [--dry-run]

Reads each file and its rotated copies (oldest first). Every event gets the same deterministic event_id
the live writer gives it (audit_db.event_id), and inserts use ON CONFLICT (event_id) DO NOTHING, so events
already in the database, written live or by an earlier run, are skipped. Files are only read, never
changed. Validation events carry no timestamp; their created_at is the source file's modification time.
Prints counts only, never event content.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from app.services import audit_db  # noqa: E402

FILES = {"generation": "report_generation_audit.jsonl", "coverage": "report_coverage_audit.jsonl",
         "validation": "report_validation.jsonl"}
BATCH = 500


def sources(directory: Path, name: str) -> list[Path]:
    rotated = [p for p in directory.glob(name + ".*") if p.suffix[1:].isdigit()]
    base = directory / name
    return sorted(rotated, key=lambda p: -int(p.suffix[1:])) + ([base] if base.exists() else [])


def read_events(path: Path) -> tuple[list[dict], int]:
    events, bad = [], 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(item, dict):
                events.append(audit_db.normalize(item))
            else:
                bad += 1
    return events, bad


def migrate(conn, directory: Path, schema: str = "audit", dry_run: bool = False) -> dict[str, dict[str, int]]:
    totals = {}
    for kind, name in FILES.items():
        stats = totals[kind] = {"files": 0, "events": 0, "unreadable_lines": 0, "inserted": 0, "already_present": 0}
        for path in sources(directory, name):
            events, bad = read_events(path)
            fallback = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            stats["files"] += 1
            stats["events"] += len(events)
            stats["unreadable_lines"] += bad
            for start in range(0, len(events), BATCH):
                chunk = [(kind, event, fallback) for event in events[start:start + BATCH]]
                if dry_run:
                    for item in chunk:  # Validates every row can be built.
                        audit_db.build_row(*item)
                    continue
                inserted = audit_db.insert_events(conn, chunk, schema)
                conn.commit()
                stats["inserted"] += inserted
                stats["already_present"] += len(chunk) - inserted
    return totals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent.parent / "output")
    parser.add_argument("--schema", default=os.getenv("AI_BOX_AUDIT_DB_SCHEMA", "audit"))
    parser.add_argument("--dry-run", action="store_true", help="Parse and map every event; write nothing.")
    args = parser.parse_args()
    url = os.getenv("DATABASE_URL", "").strip()
    if not url and not args.dry_run:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 2
    if args.dry_run:
        totals = migrate(None, args.output_dir, args.schema, dry_run=True)
    else:
        with psycopg.connect(url, connect_timeout=10, application_name="ai_box_audit_migration") as conn:
            totals = migrate(conn, args.output_dir, args.schema)
    for kind, stats in totals.items():
        print(f"{kind:<10} " + "  ".join(f"{k}={v}" for k, v in stats.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
