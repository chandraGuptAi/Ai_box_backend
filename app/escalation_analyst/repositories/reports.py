"""Saved analysis and model usage; every SQL query stays in this repository."""

from __future__ import annotations

import json
import time
from ..errors import EscalationError


class Reports:
    def __init__(self, db):
        self.db = db

    def save(self, values: dict) -> int:
        columns = [
            "group_id",
            "group_name",
            "report",
            "message_count",
            "window_label",
            "period_start",
            "period_end",
            "model",
            "trigger",
            "scope",
            "metrics_json",
            "cluster_id",
            "base_report_id",
            "max_message_id",
        ]
        return self.db.write(
            "INSERT INTO reports("
            + ",".join(columns)
            + ",created_at) VALUES("
            + ",".join("?" for _ in range(len(columns) + 1))
            + ")",
            tuple(values.get(k) for k in columns) + (int(time.time()),),
            True,
        )

    def get(self, report_id: int) -> dict:
        rows = self.db.read("SELECT * FROM reports WHERE id=?", (report_id,))
        if not rows:
            raise EscalationError(404, "Report not found")
        return rows[0]

    def history(
        self, group_id: str | None = None, scope: str = "all", search: str = "", window: dict | None = None
    ) -> list[dict]:
        conditions, args = [], []
        if group_id:
            conditions.append("r.group_id=?")
            args.append(group_id)
        if scope != "all":
            conditions.append("r.scope=?")
            args.append(scope)
        if search:
            conditions.append("(LOWER(r.group_name) LIKE ? OR LOWER(r.report) LIKE ?)")
            args.extend(("%" + search.lower() + "%",) * 2)
        if window:
            conditions.append("r.created_at>=? AND r.created_at<?")
            args.extend((window["from"], window["to"]))
        return self.db.read(
            "SELECT r.*,COALESCE(o.name,l.name,r.group_name,r.group_id) AS display_name FROM reports r LEFT JOIN group_name_overrides o ON r.group_id=o.group_id LEFT JOIN group_names_learned l ON r.group_id=l.group_id"
            + (" WHERE " + " AND ".join(conditions) if conditions else "")
            + " ORDER BY r.created_at DESC,r.id DESC",
            tuple(args),
        )

    def context(self, group_id: str, before: int, limit: int) -> list[dict]:
        return self.db.read(
            "SELECT * FROM reports WHERE group_id=? AND period_end<=? ORDER BY period_end DESC,id DESC LIMIT ?",
            (group_id, before, limit),
        )

    def base(self, group_id: str, window: dict, messages: list[dict]) -> dict | None:
        rows = self.db.read(
            "SELECT * FROM reports WHERE group_id=? AND period_start<=? AND period_end<=? ORDER BY period_end DESC,id DESC",
            (group_id, window["from"], window["to"]),
        )
        for row in rows:
            if row["period_start"] == window["from"]:
                return row
            if window.get("rolling") and row["window_label"] == window["label"]:
                lost = self.db.read(
                    "SELECT COUNT(*) AS n FROM messages WHERE group_id=? AND timestamp>=? AND timestamp<?",
                    (group_id, row["period_start"], window["from"]),
                )[0]["n"]
                if not lost:
                    return row
        return None

    def usage(self, values: dict) -> None:
        keys = (
            "scope",
            "group_id",
            "group_name",
            "model",
            "input_tokens",
            "output_tokens",
            "cost_usd",
            "cost_inr",
        )
        self.db.write(
            "INSERT INTO usage_log(ts,"
            + ",".join(keys)
            + ") VALUES("
            + ",".join("?" for _ in range(9))
            + ")",
            (int(time.time()),) + tuple(values.get(k) for k in keys),
        )

    def usage_log(self, limit: int) -> list[dict]:
        return self.db.read("SELECT * FROM usage_log ORDER BY ts DESC,id DESC LIMIT ?", (limit,))

    def usage_totals(self, since: int = 0) -> dict:
        return self.db.read(
            "SELECT COUNT(*) AS calls,COALESCE(SUM(input_tokens),0) AS input_tokens,COALESCE(SUM(output_tokens),0) AS output_tokens,COALESCE(SUM(cost_usd),0) AS cost_usd,COALESCE(SUM(cost_inr),0) AS cost_inr FROM usage_log WHERE ts>=?",
            (since,),
        )[0]
