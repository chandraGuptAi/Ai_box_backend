"""History and master analysis endpoints."""

from __future__ import annotations

import time
from fastapi import APIRouter, Depends, Query, Response
from .dependencies import runtime, user
from .models import AnalysisInput
from ..utils.ist import date_filter, day, midnight, resolve_window
from ..exports.documents import export

router = APIRouter(dependencies=[Depends(user)], tags=["Escalation reports"])


@router.get("/reports/history/summary")
def summary(r=Depends(runtime)) -> dict:
    reports = r.reports.history()
    return dict(
        total_reports=len(reports),
        reports_today=sum(row["created_at"] >= midnight(day(time.time())) for row in reports),
        groups_analyzed=len({row["group_id"] for row in reports if row["scope"] == "group"}),
        latest_report=reports[0] if reports else None,
    )


@router.get("/reports/history")
def history(
    scope: str = Query("all", pattern="^(all|group|master)$"),
    search: str = Query("", max_length=200),
    fromDate: str | None = None,
    toDate: str | None = None,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    r=Depends(runtime),
) -> dict:
    rows = r.reports.history(scope=scope, search=search, window=date_filter(fromDate, toDate))
    return dict(reports=rows[offset : offset + limit], total=len(rows), limit=limit, offset=offset)


@router.get("/reports/{report_id}")
def report(report_id: int, r=Depends(runtime)) -> dict:
    return {"report": r.reports.get(report_id)}


@router.get("/reports/{report_id}/download")
def download(report_id: int, format: str = "md", r=Depends(runtime)) -> Response:
    content, media = export(r.reports.get(report_id)["report"], format)
    return Response(
        content,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="report-{report_id}.{format}"'},
    )


@router.get("/master/reports")
def masters(clusterId: str = "all", days: int = Query(7, ge=1, le=90), r=Depends(runtime)) -> dict:
    rows = r.reports.history(
        "__master__:" + clusterId,
        window={"from": int(time.time()) - days * 86400, "to": int(time.time()) + 1},
    )
    running = next((j for j in r.jobs.list("master") if j["status"] == "running"), None)
    return dict(reports=rows, lock={"running": bool(running)}, newSince=None)


@router.get("/master/context-suggestions")
def context(
    clusterId: str = "all", beforeTs: int = Query(ge=0), limit: int = Query(2, ge=1, le=5), r=Depends(runtime)
) -> dict:
    return {"suggestions": r.reports.context("__master__:" + clusterId, beforeTs, limit)}


@router.post("/master/analyse")
def master(body: AnalysisInput, r=Depends(runtime)) -> dict:
    options = body.model_dump(exclude_none=True)
    r.clusters.get(body.clusterId, active=True)
    if not options.get("hours") and not options.get("fromDate"):
        options["mode"] = "today-so-far"
    options.update(resolve_window(options))
    job, created = r.start("master", "master", options, lambda job_id: r.analysis.master(options, job_id))
    return dict(runId=job["id"], status="started" if created else "in_progress")


@router.get("/master/progress")
def progress(runId: int | None = None, clusterId: str = "all", r=Depends(runtime)) -> dict:
    rows = (
        [r.jobs.get(runId, "master")]
        if runId
        else [j for j in r.jobs.list("master") if j["request"].get("clusterId", "all") == clusterId]
    )
    job = rows[0] if rows else None
    run = (
        {**(job["result"] or {}), "id": job["id"], "status": job["status"], "error": job["error"]}
        if job
        else None
    )
    return dict(run=run, lock={"running": any(j["status"] == "running" for j in r.jobs.list("master"))})


@router.get("/master/bulk-export")
def bulk_export(
    reportId: int | None = None,
    runId: int | None = None,
    clusterId: str = "all",
    fromDate: str | None = None,
    toDate: str | None = None,
    r=Depends(runtime),
) -> Response:
    from ..exports.bulk import export_bulk

    window = date_filter(fromDate, toDate)
    if reportId is not None:
        selected = [r.reports.get(reportId)]
    else:
        if runId is not None:
            job = r.jobs.get(runId, "master")
            window = {"from": job["request"]["from"], "to": job["request"]["to"] + 1}
        ids = {g["group_id"] for g in r.scope_groups("cluster", clusterId)}
        selected = [
            row
            for row in r.reports.history(scope="group")
            if row["group_id"] in ids
            and (not window or row["period_start"] >= window["from"] and row["period_end"] < window["to"])
        ]
        latest = {}
        for row in selected:
            latest.setdefault(row["group_id"], row)
        selected = list(latest.values())
    return Response(
        export_bulk(selected),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="master_escalations.xlsx"'},
    )
