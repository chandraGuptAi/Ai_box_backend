"""Opt-in IST scheduler; database request keys prevent duplicate runs across workers."""

from __future__ import annotations
from datetime import datetime
import logging
from .utils.ist import IST, day, midnight, resolve_window

logger = logging.getLogger("escalation_analyst.scheduler")


def tick(runtime, now: int) -> None:
    stamp = datetime.fromtimestamp(now, IST)
    minute = stamp.strftime("%H:%M")
    for schedule in runtime.preferences.schedules():
        configured = ":".join(part.zfill(2) for part in schedule["time_hhmm"].split(":"))
        if not schedule["enabled"] or configured != minute:
            continue
        options = dict(
            clusterId="all",
            mode=schedule["window_mode"],
            trigger="schedule",
            requestKey=f"schedule:{schedule['id']}:{stamp.date().isoformat()}:{minute}",
        )
        options.update(resolve_window(options, now))
        runtime.start(
            "master",
            "master",
            options,
            lambda job_id, options=options: runtime.analysis.master(options, job_id),
        )
    # Housekeeping has a daily durable claim and never deletes reports.
    if runtime.settings.retention_days > 0:
        key = "retention:" + day(now)
        runtime.start(
            "retention",
            "retention",
            {"requestKey": key},
            lambda _: dict(messages=runtime.groups.purge(runtime.settings.retention_days)),
        )


def run(runtime) -> None:
    import time

    while not runtime.stop.is_set():
        try:
            tick(runtime, int(time.time()))
        except Exception:
            logger.exception("Escalation scheduler tick failed")
        runtime.stop.wait(20)
