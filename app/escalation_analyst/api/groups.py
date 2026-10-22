"""Group, conversation and analysis endpoints; business rules live in services."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from .dependencies import runtime, user
from .models import Name, Status, Assignment, AnalysisInput
from ..utils.ist import date_filter, resolve_window

router = APIRouter(dependencies=[Depends(user)], tags=["Escalation groups"])


@router.get("/groups")
def groups(fromDate: str | None = None, toDate: str | None = None, r=Depends(runtime)) -> dict:
    return {"groups": r.groups.list(date_filter(fromDate, toDate))}


@router.post("/groups/refresh-names")
def refresh(r=Depends(runtime)) -> dict:
    return r.sidecar.call("POST", "/refresh-names")


@router.patch("/groups/{group_id}/status")
def status(group_id: str, body: Status, r=Depends(runtime)) -> dict:
    return {"group": r.groups.status(group_id, body.status == "active")}


@router.post("/groups/{group_id}/name")
def name(group_id: str, body: Name, r=Depends(runtime)) -> dict:
    r.groups.name(group_id, body.name)
    return {"ok": True, "name": body.name}


@router.post("/groups/{group_id}/cluster")
def assign(group_id: str, body: Assignment, r=Depends(runtime)) -> dict:
    g = r.groups.get(group_id)
    r.clusters.assign(group_id, body.clusterId, body.groupName or g["group_name"])
    return {"ok": True}


@router.delete("/groups/{group_id}")
def delete(group_id: str, r=Depends(runtime)) -> dict:
    return r.groups.delete(group_id)


@router.get("/groups/{group_id}/messages")
def messages(
    group_id: str,
    fromDate: str | None = None,
    toDate: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    r=Depends(runtime),
) -> dict:
    r.groups.get(group_id)
    rows = r.groups.messages(group_id, date_filter(fromDate, toDate))
    return dict(
        messages=list(reversed(rows))[offset : offset + limit],
        summary={"total": len(rows), "last_message_ts": rows[-1]["timestamp"] if rows else None},
        hasMore=offset + limit < len(rows),
    )


@router.get("/groups/{group_id}/reports")
def reports(
    group_id: str,
    days: int = Query(7, ge=1, le=90),
    fromDate: str | None = None,
    toDate: str | None = None,
    r=Depends(runtime),
) -> dict:
    import time

    window = date_filter(fromDate, toDate) or {
        "from": int(time.time()) - days * 86400,
        "to": int(time.time()) + 1,
    }
    return {"reports": r.reports.history(group_id, window=window)}


@router.get("/groups/{group_id}/context-suggestions")
def context(
    group_id: str, beforeTs: int = Query(ge=0), limit: int = Query(2, ge=1, le=5), r=Depends(runtime)
) -> dict:
    return {"suggestions": r.reports.context(group_id, beforeTs, limit)}


@router.post("/groups/{group_id}/analyse")
def analyse(group_id: str, body: AnalysisInput, r=Depends(runtime)) -> dict:
    r.groups.get(group_id)
    options = body.model_dump(exclude_none=True)
    resolve_window(options)
    job, created = r.start(
        "group", "group:" + group_id, options, lambda _: r.analysis.group(group_id, options)
    )
    return {"status": "started" if created else "in_progress", "startedAt": job["created_at"]}


@router.get("/groups/{group_id}/analyse/status")
def analysis_status(group_id: str, r=Depends(runtime)) -> dict:
    jobs = r.jobs.list("group", "group:" + group_id)
    if not jobs:
        return {"status": "idle"}
    job = jobs[0]
    return {
        **(job["result"] or {}),
        "status": "done" if job["status"] == "complete" else job["status"],
        "startedAt": job["created_at"],
        "finishedAt": None if job["status"] == "running" else job["updated_at"],
        "error": job["error"],
    }
