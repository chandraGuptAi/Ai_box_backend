"""Import historical deterministic snapshots with a stable mapping from Node UUIDs."""

from __future__ import annotations
import json
import time


def import_job(target, kind: str, row: dict, snapshot: dict, request: dict) -> int:
    legacy_id = str(row["id"])
    request_key = "legacy:" + kind + ":" + legacy_id
    old = target.read("SELECT id FROM jobs WHERE kind=? AND request_key=?", (kind, request_key))
    if old:
        return old[0]["id"]
    status = row.get("status", "error")
    error = row.get("error")
    if status == "running":
        status = "error"
        error = "Interrupted before Python migration"
    request = {**request, "legacyId": legacy_id}
    result = {"snapshot": snapshot, "request": request} if kind == "performance" else snapshot
    return target.write(
        "INSERT INTO jobs(lock_key,kind,status,request_key,request,result,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            kind,
            kind,
            status,
            request_key,
            json.dumps(request),
            json.dumps(result),
            error,
            row.get("created_at") or int(time.time()),
            row.get("finished_at") or row.get("created_at") or int(time.time()),
        ),
        True,
    )


def import_snapshot(
    target, source, postgres: bool, schema: str, table: str, row: dict, owner_id: str
) -> None:
    if table == "performance_reports":
        result = json.loads(row["result_json"]) if row.get("result_json") else {}
        request = json.loads(row["request_json"])
        import_job(target, "performance", row, result, request)
    elif table == "logistics_reports":
        name = f'"{schema}"."logistics_records"' if postgres else "logistics_records"
        placeholder = "%s" if postgres else "?"
        records = []
        for record in source.execute(
            f"SELECT * FROM {name} WHERE report_id={placeholder} ORDER BY record_id", (row["id"],)
        ):
            value = json.loads(record["snapshot_json"])
            value["recordId"] = str(record["record_id"])
            records.append(value)
        snapshot = dict(
            records=records,
            summary=json.loads(row["summary_json"]) if row.get("summary_json") else None,
            quality=json.loads(row["quality_json"]) if row.get("quality_json") else None,
        )
        import_job(target, "logistics", row, snapshot, json.loads(row["request_json"]))
    elif table == "report_presets":
        now = int(time.time())
        legacy_id = str(row["id"])
        existing = target.read(
            "SELECT 1 FROM legacy_imports WHERE kind=? AND legacy_id=?", ("preset", legacy_id)
        )
        if not existing:
            created = target.write(
                "INSERT INTO report_presets(owner_id,name,config,created_at,updated_at) VALUES(?,?,?,?,?)",
                (
                    owner_id,
                    row["name"],
                    row["config_json"],
                    row.get("created_at") or now,
                    row.get("updated_at") or now,
                ),
                True,
            )
            target.write(
                "INSERT INTO legacy_imports(kind,legacy_id,target_id) VALUES(?,?,?)",
                ("preset", legacy_id, created),
            )
