"""Logistics generation, persisted results, evidence paging and XLSX export."""

from __future__ import annotations

from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, Query, Response
from pydantic import Field
from .dependencies import runtime, user
from .models import Input
from ..errors import EscalationError
from ..utils.ist import date_filter
from ..logistics.engine import build_report
from ..logistics.workbook import export_workbook

router = APIRouter(dependencies=[Depends(user)], tags=["Escalation logistics"])


class Generate(Input):
    fromDate: str
    toDate: str
    scope: Literal["all", "group", "cluster"] = "all"
    scopeId: str | None = None
    mode: Literal["enhanced", "legacy"] = "enhanced"
    view: Literal["history", "daily"] = "history"
    requestKey: UUID


def ready(r, job_id: int) -> dict:
    job = r.jobs.get(job_id, "logistics")
    if job["status"] != "complete":
        raise EscalationError(409, "Report is not complete")
    return job


def public(job: dict) -> dict:
    result = job["result"] or {}
    return dict(
        id=job["id"],
        created_at=job["created_at"],
        status=job["status"],
        request=job["request"],
        summary=result.get("summary"),
        quality=result.get("quality"),
        error=job["error"],
        finished_at=job["updated_at"] if job["status"] != "running" else None,
    )


@router.post("/logistics-reports/generate", status_code=202)
def generate(body: Generate, response: Response, r=Depends(runtime)) -> dict:
    request = body.model_dump(mode="json")
    window = date_filter(body.fromDate, body.toDate)
    if window["to"] - window["from"] > 31 * 86400:
        raise EscalationError(400, "Select at most 31 days")
    if body.scope != "all" and not body.scopeId:
        raise EscalationError(400, "Choose a group or cluster")
    ids = [g["group_id"] for g in r.scope_groups(body.scope, body.scopeId or "")]

    def work(_: int) -> dict:
        result = build_report(r.groups.messages(None, window, ids), {**request, **window})
        for index, record in enumerate(result["records"]):
            record["recordId"] = str(index + 1)
        return result

    job, created = r.start("logistics", "logistics", request, work)
    if not created:
        if job["request"].get("requestKey") != request["requestKey"]:
            raise EscalationError(409, f"Another logistics run is processing (reportId={job['id']})")
        response.status_code = 200
    return public(job)


@router.get("/logistics-reports")
def history(limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0), r=Depends(runtime)) -> dict:
    rows = r.jobs.list("logistics")
    return dict(
        reports=[public(j) for j in rows[offset : offset + limit]],
        total=len(rows),
        limit=limit,
        offset=offset,
    )


@router.get("/logistics-reports/{job_id}")
def detail(job_id: int, r=Depends(runtime)) -> dict:
    return public(r.jobs.get(job_id, "logistics"))


@router.get("/logistics-reports/{job_id}/records")
def records(
    job_id: int,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    search: str = Query("", max_length=200),
    mile: str = "",
    status: str = "",
    sort: Literal["first", "latest", "awb"] = "first",
    fromDate: str | None = None,
    toDate: str | None = None,
    r=Depends(runtime),
) -> dict:
    window = date_filter(fromDate, toDate)
    rows = [
        row
        for row in ready(r, job_id)["result"]["records"]
        if (not search or search.lower() in (row["awb"] + " " + row["groupName"]).lower())
        and (not mile or row["mile"] == mile)
        and (not status or row["status"] == status)
        and (not window or window["from"] <= row["firstAt"] < window["to"])
    ]
    rows.sort(
        key=lambda row: (
            row["awb"] if sort == "awb" else row["lastAt"] if sort == "latest" else row["firstAt"]
        ),
        reverse=sort == "latest",
    )
    return dict(
        records=[
            {k: v for k, v in row.items() if k not in ("sourceMessages", "observations")}
            for row in rows[offset : offset + limit]
        ],
        total=len(rows),
        limit=limit,
        offset=offset,
    )


@router.get("/logistics-reports/{job_id}/records/{record_id}")
def evidence(
    job_id: int,
    record_id: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    r=Depends(runtime),
) -> dict:
    row = next((row for row in ready(r, job_id)["result"]["records"] if row["recordId"] == record_id), None)
    if not row:
        raise EscalationError(404, "Record not found")
    return {
        **row,
        "sourceTotal": len(row["sourceMessages"]),
        "evidenceOffset": offset,
        "evidenceLimit": limit,
        "sourceMessages": row["sourceMessages"][offset : offset + limit],
        "observations": row["observations"][offset : offset + limit],
    }


@router.get("/logistics-reports/{job_id}/export")
def download(job_id: int, r=Depends(runtime)) -> Response:
    job = ready(r, job_id)
    return Response(
        export_workbook(job["result"]["records"], job["request"]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="Logistics_Dump_{job_id}.xlsx"'},
    )
