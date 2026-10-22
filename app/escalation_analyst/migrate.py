"""Read-only-by-default Node SQLite/PostgreSQL import into the isolated module database."""

from __future__ import annotations
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import time
from .settings import Settings
from .repositories.database import Database
from .repositories.legacy import import_snapshot

CORE = [
    "whatsapp_groups",
    "group_name_overrides",
    "group_names_learned",
    "messages",
    "clusters",
    "group_clusters",
    "schedules",
    "reports",
    "usage_log",
    "group_activity_policy",
    "api_keys",
]
ARCHIVE = CORE + [
    "report_runs",
    "logistics_reports",
    "logistics_records",
    "performance_reports",
    "report_presets",
]


@contextmanager
def source_connection(sqlite_path: str | None, postgres_url: str | None, schema: str):
    if sqlite_path:
        path = Path(sqlite_path).resolve()
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            yield conn, False
        finally:
            conn.close()
    else:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(
            postgres_url, row_factory=dict_row, options="-c default_transaction_read_only=on"
        ) as conn:
            yield conn, True


def exists(source, postgres: bool, schema: str, table: str) -> bool:
    if postgres:
        return bool(
            source.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s",
                (schema, table),
            ).fetchone()
        )
    return bool(
        source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    )


def target_columns(db: Database, table: str) -> list[str]:
    if db.postgres:
        return [
            r["column_name"]
            for r in db.read(
                "SELECT column_name FROM information_schema.columns WHERE table_schema=? AND table_name=? ORDER BY ordinal_position",
                ("escalation_analyst", table),
            )
        ]
    return [r["name"] for r in db.read("PRAGMA table_info(" + table + ")")]


def migrate(
    sqlite_path: str | None = None,
    postgres_url: str | None = None,
    schema: str = "public",
    apply: bool = False,
    owner_id: str = "legacy-migration",
    settings: Settings | None = None,
) -> dict:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise ValueError("Invalid source schema")
    target = Database(settings or Settings())
    counts = {}
    with source_connection(sqlite_path, postgres_url, schema) as (source, postgres):
        for table in ARCHIVE:
            if not exists(source, postgres, schema, table):
                continue
            qualified = '"' + schema + '"."' + table + '"' if postgres else '"' + table + '"'
            if not apply:
                counts[table] = source.execute("SELECT COUNT(*) AS n FROM " + qualified).fetchone()["n"]
                continue
            columns = target_columns(target, table) if table in CORE else []
            counts[table] = 0
            for raw in source.execute("SELECT * FROM " + qualified):
                row = dict(raw)
                identity = str(row.get("id") or row.get("group_id") or counts[table])
                if table == "logistics_records":
                    identity = str(row["report_id"]) + ":" + str(row["record_id"])
                # Full source rows are archived so fields outside the new public API are not lost.
                target.write(
                    "INSERT INTO legacy_records(table_name,record_id,payload) VALUES(?,?,?) ON CONFLICT(table_name,record_id) DO NOTHING",
                    (table, identity, json.dumps(row, default=str)),
                )
                if columns:
                    values = {k: row[k] for k in columns if k in row}
                    if table == "group_activity_policy":
                        values["id"] = 1
                        target.write(
                            "UPDATE group_activity_policy SET days=?,enabled=? WHERE id=1",
                            (row["days"], row["enabled"]),
                        )
                    if "created_at" in columns and not values.get("created_at"):
                        values["created_at"] = int(time.time())
                    if table == "api_keys" and "key_hash" not in values:
                        values["key_hash"] = row.get("hash")
                    names = list(values)
                    if names:
                        target.write(
                            "INSERT INTO "
                            + table
                            + "("
                            + ",".join(names)
                            + ") VALUES("
                            + ",".join("?" for _ in names)
                            + ") ON CONFLICT DO NOTHING",
                            tuple(values[k] for k in names),
                        )
                if table in ("logistics_reports", "performance_reports", "report_presets"):
                    import_snapshot(target, source, postgres, schema, table, row, owner_id)
                counts[table] += 1
        if apply:
            # Populate the group directory even when the source predates its registry table.
            target.write(
                "INSERT INTO whatsapp_groups(group_id,group_name) SELECT group_id,MAX(group_name) FROM messages GROUP BY group_id ON CONFLICT(group_id) DO NOTHING"
            )
            if target.postgres:
                for table in ("messages", "reports", "schedules", "usage_log", "api_keys"):
                    target.write(
                        "SELECT setval(pg_get_serial_sequence(?, 'id'), COALESCE((SELECT MAX(id) FROM "
                        + table
                        + "),1), EXISTS(SELECT 1 FROM "
                        + table
                        + "))",
                        ("escalation_analyst." + table,),
                    )
    return {
        "applied": apply,
        "tables": counts,
        "legacy_archive": (
            "Full source rows preserved in legacy_records. Deterministic reports receive new numeric IDs; their requests retain legacyId."
            if apply
            else None
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--source-sqlite")
    group.add_argument("--source-postgres")
    parser.add_argument("--source-schema", default="public")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--owner-id", default="legacy-migration", help="Owner of imported report-builder presets"
    )
    args = parser.parse_args()
    print(
        json.dumps(
            migrate(args.source_sqlite, args.source_postgres, args.source_schema, args.apply, args.owner_id),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
