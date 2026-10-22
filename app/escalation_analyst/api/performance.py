"""Performance snapshots, report-builder options, filters, exports and owner-scoped presets."""

from __future__ import annotations
import json
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import Field
from .dependencies import runtime, user
from .models import Input, Preset
from ..errors import EscalationError
from ..performance.engine import SHEETS
from ..performance.mappings import validate_mappings
from ..performance import service, details
from ..performance.workbook import export_workbook

router = APIRouter(dependencies=[Depends(user)], tags=["Escalation performance"])


class Generate(Input):
    type: Literal["performance", "overview", "kam", "seller", "awb", "activity"] = "performance"
    mode: Literal["summary", "detailed", "conversation"] = "detailed"
    period: Literal["today", "yesterday", "last24", "7", "mtd", "previous", "custom"] = "today"
    fromDate: str | None = None
    toDate: str | None = None
    groupIds: list[str] = Field(default_factory=list, max_length=500)
    employeeIds: list[str] = Field(default_factory=list, max_length=500)
    sellerIds: list[str] = Field(default_factory=list, max_length=2000)
    categories: list[str] = Field(default_factory=list, max_length=100)
    observed: list[str] = Field(default_factory=list, max_length=100)
    statuses: list[str] = Field(default_factory=list, max_length=100)
    miles: list[str] = Field(default_factory=list, max_length=100)
    sections: list[str] = Field(default_factory=lambda: [k for k, _ in details.SECTIONS], max_length=12)
    groupBy: Literal["none", "group", "date", "kam", "seller", "mile", "category", "observed", "erp"] = "none"
    sort: str = Field(default="firstAt", max_length=50)
    direction: Literal["asc", "desc"] = "asc"
    columns: list[str] = Field(default_factory=list, max_length=30)
    force: bool = False
    mappings: dict = Field(default_factory=dict)
    mappingsFrom: int | None = None
    requestKey: UUID


def query_filters(request: Request) -> dict:
    keys = [
        "groupIds",
        "groupId",
        "employeeIds",
        "employeeId",
        "sellerIds",
        "sellerId",
        "categories",
        "statuses",
        "status",
        "observed",
        "miles",
        "mile",
    ]
    result = {
        k: [value for entry in request.query_params.getlist(k) for value in entry.split(",") if value]
        for k in keys
    }
    result.update({k: request.query_params.get(k, "") for k in ("awb", "method")})
    return result


def snapshot(r, job_id: int, request: Request) -> tuple[dict, dict, dict]:
    job = r.jobs.get(job_id, "performance")
    if job["status"] != "complete":
        raise EscalationError(409, "Report is not complete")
    source = job["result"]
    config = source["request"]
    result = service.filtered(source["snapshot"], config, query_filters(request))
    return job, result, config


@router.get("/performance-reports/options")
def options(r=Depends(runtime)) -> dict:
    return dict(
        periods=["today", "yesterday", "last24", "7", "mtd", "previous", "custom"],
        groups=[{"id": g["group_id"], "name": g["group_name"]} for g in r.groups.list()],
        categories=sorted(
            {
                row["category"]
                for job in r.jobs.list("logistics")
                if job["result"]
                for row in job["result"].get("records", [])
            }
        ),
        observed=details.OBSERVED,
        statuses=details.STATUSES,
        miles=details.MILES,
        sections=details.SECTIONS,
        modes=details.MODES,
        groupBy=details.GROUP_BY,
        sheets=SHEETS,
        mappings={"employees": [], "sellers": [], "groups": [], "members": []},
    )


@router.post("/performance-reports/validate-mappings")
def validate(body: dict) -> dict:
    if len(json.dumps(body).encode()) > 8 * 1024 * 1024:
        raise EscalationError(400, "Mappings exceed 8MB")
    return {"counts": {key: len(rows) for key, rows in validate_mappings(body).items()}}


@router.post("/performance-reports/generate")
def generate(body: Generate, r=Depends(runtime)) -> dict:
    config = body.model_dump(mode="json", exclude_none=True)
    service.period_window(config)
    if body.mappingsFrom:
        previous = r.jobs.get(body.mappingsFrom, "performance")
        if previous["status"] != "complete":
            raise EscalationError(409, "Mapping source report is not complete")
        config["mappings"] = previous["result"]["request"]["mappings"]
    validate_mappings(config["mappings"])
    if set(config["sections"]) - {k for k, _ in details.SECTIONS}:
        raise EscalationError(400, "Unknown report section")
    job, created = r.start("performance", "performance", config, lambda _: service.generate(r, config))
    if not created and job["request"].get("requestKey") != config["requestKey"]:
        raise EscalationError(409, f"Another performance run is processing (reportId={job['id']})")
    return dict(id=job["id"], reused=not created, reuseReason="requestKey" if not created else None)


def public(job: dict) -> dict:
    result = job["result"] or {}
    snapshot = result.get("snapshot", {})
    return dict(
        id=job["id"],
        created_at=job["created_at"],
        finished_at=job["updated_at"] if job["status"] != "running" else None,
        status=job["status"],
        request=job["request"],
        summary=snapshot.get("summary"),
        error=job["error"],
        sheets=SHEETS,
        sections=job["request"].get("sections", []),
        groupBy=job["request"].get("groupBy", "none"),
    )


@router.get("/performance-reports")
def history(limit: int = Query(10, ge=1, le=100), offset: int = Query(0, ge=0), r=Depends(runtime)) -> dict:
    rows = r.jobs.list("performance")
    return dict(
        reports=[public(j) for j in rows[offset : offset + limit]],
        total=len(rows),
        offset=offset,
        limit=limit,
    )


@router.get("/performance-reports/{job_id}")
def report(job_id: int, r=Depends(runtime)) -> dict:
    return public(r.jobs.get(job_id, "performance"))


@router.get("/performance-reports/{job_id}/details")
def preview(
    job_id: int,
    request: Request,
    groupBy: str = Query("none", pattern="^(none|group|date|kam|seller|mile|category|observed|erp)$"),
    r=Depends(runtime),
) -> dict:
    job, result, config = snapshot(r, job_id, request)
    value = details.build_details(result, config, groupBy)
    source = r.groups.messages(None, config, config["groupIds"])
    latest = max((m["id"] for m in source), default=None)
    coverage = result.get("coverage", {})
    stale = (
        service.fingerprint(source) != coverage["fingerprint"]
        if coverage.get("fingerprint")
        else latest != coverage.get("maxMessageId")
    )
    value["freshness"] = {
        "stale": stale,
        "changed": stale,
        "newMessages": sum(m["id"] > (result.get("coverage", {}).get("maxMessageId") or 0) for m in source),
    }
    return value


@router.get("/performance-reports/{job_id}/conversations")
def conversations(
    job_id: int,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    search: str = Query("", max_length=200),
    sort: str = "firstAt",
    direction: Literal["asc", "desc"] = "asc",
    r=Depends(runtime),
) -> dict:
    _, result, config = snapshot(r, job_id, request)
    rows = [
        row
        for row in details.conversations(result, config["mappings"])
        if search.lower() in (row["awb"] + " " + row["groupName"]).lower()
    ]
    key = sort if sort in ("firstAt", "lastAt", "awb", "groupName", "entries", "days") else "firstAt"
    rows.sort(key=lambda row: row[key], reverse=direction == "desc")
    return dict(
        rows=[details.public_conversation(row) for row in rows[offset : offset + limit]],
        total=len(rows),
        offset=offset,
        limit=limit,
    )


@router.get("/performance-reports/{job_id}/timeline")
def timeline(
    job_id: int, request: Request, groupId: str, awb: str = Query(pattern=r"^\d{8,16}$"), r=Depends(runtime)
) -> dict:
    _, result, config = snapshot(r, job_id, request)
    row = details.timeline(result, config, groupId, awb)
    if not row:
        raise EscalationError(404, "Conversation not found")
    return row


@router.get("/performance-reports/{job_id}/table")
def table(
    job_id: int,
    request: Request,
    sheet: str = "Overview",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    search: str = Query("", max_length=200),
    sortColumn: int | None = None,
    direction: Literal["asc", "desc"] = "asc",
    r=Depends(runtime),
) -> dict:
    _, result, _ = snapshot(r, job_id, request)
    if sheet not in result["sheets"]:
        raise EscalationError(400, "Unknown sheet")
    data = result["sheets"][sheet]
    rows = [
        row
        for row in data["rows"]
        if not search or any(search.lower() in str(value).lower() for value in row if value is not None)
    ]
    if sortColumn is not None:
        if not 0 <= sortColumn < len(data["columns"]):
            raise EscalationError(400, "Invalid sort column")
        available = [row for row in rows if row[sortColumn] is not None]
        missing = [row for row in rows if row[sortColumn] is None]
        available.sort(
            key=lambda row: (isinstance(row[sortColumn], str), row[sortColumn]), reverse=direction == "desc"
        )
        rows = available + missing
    return {
        **data,
        "rows": rows[offset : offset + limit],
        "total": len(rows),
        "offset": offset,
        "limit": limit,
        "summary": result["summary"],
    }


@router.get("/performance-reports/{job_id}/export")
def download(
    job_id: int, request: Request, sheet: str | None = None, sheets: str | None = None, r=Depends(runtime)
) -> Response:
    job, result, config = snapshot(r, job_id, request)
    selected = [sheet] if sheet else sheets.split(",") if sheets else None
    if selected and any(name not in SHEETS for name in selected):
        raise EscalationError(400, "Unknown sheet")
    columns = {}
    if request.query_params.get("columns"):
        try:
            columns = json.loads(request.query_params["columns"])
            valid = isinstance(columns, dict) and all(
                name in SHEETS
                and isinstance(indexes, list)
                and indexes
                and all(
                    type(index) is int and 0 <= index < len(result["sheets"][name]["columns"])
                    for index in indexes
                )
                for name, indexes in columns.items()
            )
            if not valid:
                raise ValueError()
        except (ValueError, TypeError):
            raise EscalationError(400, "Invalid workbook columns")
    content = export_workbook(result, {"created_at": job["created_at"], "request": config}, selected, columns)
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="Escalation_Performance_{job_id}.xlsx"'},
    )


@router.get("/performance-reports/{job_id}/pdf")
def pdf(job_id: int, request: Request, r=Depends(runtime)) -> Response:
    from ..exports.pdf import export_pdf

    _, result, config = snapshot(r, job_id, request)
    return Response(
        export_pdf(details.build_details(result, config), details.conversations(result, config["mappings"])),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="Escalation_Performance_{job_id}.pdf"'},
    )
