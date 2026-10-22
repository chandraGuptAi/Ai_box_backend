"""Group report reuse, incremental prompts and deterministic master metrics."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import time
from ..errors import EscalationError
from .metrics import parse_metrics, sum_metrics
from ..utils.ist import IST, resolve_window

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"


def transcript(messages: list[dict]) -> str:
    rows = []
    for m in messages:
        stamp = (
            datetime.fromtimestamp(m["timestamp"], IST)
            .strftime("%d %b, %I:%M %p")
            .replace("AM", "am")
            .replace("PM", "pm")
        )
        rows.append(
            f"[{stamp}] {m.get('author_name') or m.get('author') or 'Unknown'}: {m.get('body') or ''}"
        )
    text = "\n".join(rows)
    return "...(earlier messages trimmed)...\n" + text[-600000:] if len(text) > 600000 else text


class Analysis:
    def __init__(self, runtime):
        self.r = runtime

    def group_for_master(self, group_id: str, options: dict) -> dict | None:
        # Master and dashboard requests must share the same durable per-group claim.
        while not self.r.stop.is_set():
            job, created = self.r.start(
                "group", "group:" + group_id, options, lambda _: self.group(group_id, options)
            )
            while job["status"] == "running" and not self.r.stop.wait(0.2):
                job = self.r.jobs.get(job["id"], "group")
            if self.r.stop.is_set():
                raise EscalationError(503, "Report worker is shutting down")
            if not created:
                continue  # A different window finished; acquire and resolve our own window now.
            if job["status"] == "error":
                raise EscalationError(502, job["error"] or "Group analysis failed")
            return None if job["status"] == "empty" else job["result"]
        raise EscalationError(503, "Report worker is shutting down")

    def group(self, group_id: str, options: dict) -> dict | None:
        group = self.r.groups.get(group_id)
        window = resolve_window(options)
        messages = self.r.groups.messages(group_id, {**window, "inclusive": True})
        if not messages:
            return None
        base = None if options.get("force") else self.r.reports.base(group_id, window, messages)
        fresh = messages
        if base:
            until = (
                base["period_end"]
                if base["max_message_id"] is not None
                else min(base["period_end"], base["created_at"])
            )
            max_id = (
                base["max_message_id"]
                if base["max_message_id"] is not None
                else max(m["id"] for m in messages)
            )
            fresh = [m for m in messages if m["timestamp"] > until or m["id"] > max_id]
            if not fresh:
                return dict(
                    reportId=base["id"],
                    report=base["report"],
                    messageCount=base["message_count"],
                    windowLabel=base["window_label"],
                    metrics=json.loads(base["metrics_json"]) if base["metrics_json"] else None,
                    reuse="existing",
                    baseReport={"id": base["id"], "windowLabel": base["window_label"]},
                    newMessageCount=0,
                )
        name = group["group_name"]
        user = f"Group: {name}\nWindow: {window['label']}\nTotal messages in window: {len(messages)}\n\n"
        for report_id in options.get("contextReportIds", []):
            previous = self.r.reports.get(report_id)
            if previous["group_id"] != group_id:
                continue
            user += f"Previous report ({previous['window_label']}):\n{previous['report']}\n\n"
        if base:
            user += f"MODE: INCREMENTAL UPDATE\nBase report covers: {base['window_label']} ({base['message_count']} messages, already analysed)\nBase report:\n\"\"\"\n{base['report']}\n\"\"\"\nNew transcript (messages the base report has not seen):\n"
        else:
            user += "Transcript:\n"
        user += '"""\n' + transcript(fresh) + '\n"""'
        text = self.r.provider.generate(
            (PROMPTS / "escalation-analyst.md").read_text(encoding="utf-8"),
            user,
            dict(scope="group", group_id=group_id, group_name=name),
        )
        metrics = parse_metrics(text)
        values = dict(
            group_id=group_id,
            group_name=name,
            report=text,
            message_count=len(messages),
            window_label=window["label"],
            period_start=window["from"],
            period_end=window["to"],
            model=self.r.settings.model,
            trigger=options.get("trigger", "dashboard"),
            scope="group",
            metrics_json=json.dumps(metrics) if metrics else None,
            max_message_id=max(m["id"] for m in messages),
            base_report_id=base["id"] if base else None,
        )
        report_id = self.r.reports.save(values)
        delivered = False
        if options.get("deliver") and self.r.settings.recipient:
            try:
                self.r.sidecar.send(self.r.settings.recipient, text)
                delivered = True
            except Exception:
                pass  # Saved analysis remains successful when outbound delivery fails.
        return dict(
            reportId=report_id,
            report=text,
            messageCount=len(messages),
            windowLabel=window["label"],
            metrics=metrics,
            reuse="extended" if base else None,
            baseReport={"id": base["id"], "windowLabel": base["window_label"]} if base else None,
            newMessageCount=len(fresh),
            delivered=delivered,
        )

    def master(self, options: dict, job_id: int) -> dict:
        cluster_id = options.get("clusterId", "all")
        self.r.clusters.get(cluster_id, active=True)
        window = resolve_window(options)
        groups = self.r.scope_groups("cluster", cluster_id)
        reports = []
        progress = dict(total_groups=len(groups), completed_groups=0, current_group=None, phase="groups")
        for group in groups:
            if not group["is_active"]:
                continue
            progress["current_group"] = group["group_name"]
            self.r.jobs.update(job_id, progress)
            result = self.group_for_master(group["group_id"], {**options, **window})
            if result:
                reports.append((group, result))
            progress["completed_groups"] += 1
            self.r.jobs.update(job_id, progress)
        if not reports:
            return {**progress, "phase": "done", "note": "No messages in active groups"}
        blocks = [
            f"### GROUP id={g['group_id']} name=\"{g['group_name']}\"\n(When citing this group, tag it EXACTLY as {{{{G:{g['group_id']}|{g['group_name']}}}}})\n{r['report']}"
            for g, r in reports
        ]
        progress["phase"] = "master"
        self.r.jobs.update(job_id, progress)
        prompt = (PROMPTS / "master-analyst.md").read_text(encoding="utf-8")
        # Digest large batches before the final merge rather than truncating groups.
        batches = []
        current = ""
        for block in blocks:
            if current and len(current) + len(block) > 600000:
                batches.append(current)
                current = ""
            current += block + "\n"
        if current:
            batches.append(current)
        if len(batches) > 1:
            batches = [
                self.r.provider.generate(
                    prompt,
                    "Produce a partial digest; preserve every {{G:...}} token.\n" + batch,
                    dict(scope="master", group_id="__master__:" + cluster_id, group_name=cluster_id),
                )
                for batch in batches
            ]
        text = self.r.provider.generate(
            prompt,
            f"Window: {window['label']}\nPer-group reports:\n" + "\n".join(batches),
            dict(scope="master", group_id="__master__:" + cluster_id, group_name=cluster_id),
        )
        totals = sum_metrics([r["metrics"] for _, r in reports if r.get("metrics")])
        text += "\n\n## Computed totals\n```json\n" + json.dumps(totals, indent=2) + "\n```"
        report_id = self.r.reports.save(
            dict(
                group_id="__master__:" + cluster_id,
                group_name=cluster_id,
                scope="master",
                cluster_id=cluster_id,
                report=text,
                message_count=sum(r["messageCount"] for _, r in reports),
                window_label=window["label"],
                period_start=window["from"],
                period_end=window["to"],
                model=self.r.settings.model,
                trigger=options.get("trigger", "dashboard"),
                metrics_json=json.dumps(totals),
            )
        )
        return {**progress, "phase": "done", "master_report_id": report_id}
