"""Overview, connection, usage, activity policy and schedule endpoints."""

from __future__ import annotations

import time
from fastapi import APIRouter, Depends, Query
from .dependencies import runtime, user
from .models import Policy, Schedule
from ..utils.ist import day, midnight

router = APIRouter(dependencies=[Depends(user)], tags=["Escalation workspace"])


@router.get("/overview")
def overview(r=Depends(runtime)) -> dict:
    groups = r.groups.list()
    reports = r.reports.history()
    return dict(
        summary=dict(
            tracked_groups=len(groups),
            messages_today=sum(g["today_messages"] for g in groups),
            reports_generated=len(reports),
            last_analysis=reports[0]["created_at"] if reports else None,
        ),
        recentReports=reports[:5],
    )


@router.get("/whatsapp/status")
def connection(r=Depends(runtime)) -> dict:
    return r.sidecar.status()


@router.post("/whatsapp/reconnect")
def reconnect(r=Depends(runtime)) -> dict:
    return r.sidecar.call("POST", "/reconnect")


@router.get("/usage")
def usage(limit: int = Query(200, ge=1, le=1000), r=Depends(runtime)) -> dict:
    return dict(
        rate=r.settings.usd_inr,
        total=r.reports.usage_totals(),
        today=r.reports.usage_totals(midnight(day(time.time()))),
        last24h=r.reports.usage_totals(int(time.time()) - 86400),
        log=r.reports.usage_log(limit),
    )


@router.get("/settings/group-activity")
def policy(r=Depends(runtime)) -> dict:
    return {"policy": r.groups.policy()}


@router.put("/settings/group-activity")
def save_policy(body: Policy, r=Depends(runtime)) -> dict:
    return {"policy": r.groups.set_policy(body.days, body.enabled)}


@router.get("/schedules")
def schedules(r=Depends(runtime)) -> dict:
    return {"schedules": r.preferences.schedules()}


@router.post("/schedules")
def add_schedule(body: Schedule, r=Depends(runtime)) -> dict:
    r.preferences.save_schedule(body.model_dump())
    return {"ok": True}


@router.put("/schedules/{schedule_id}")
def update_schedule(schedule_id: int, body: Schedule, r=Depends(runtime)) -> dict:
    r.preferences.save_schedule(body.model_dump(), schedule_id)
    return {"ok": True}


@router.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: int, r=Depends(runtime)) -> dict:
    r.preferences.delete_schedule(schedule_id)
    return {"ok": True}
