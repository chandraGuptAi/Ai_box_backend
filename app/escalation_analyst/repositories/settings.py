"""Schedule and per-user preset persistence."""

from __future__ import annotations

import json
import time
from ..errors import EscalationError


class Preferences:
    def __init__(self, db):
        self.db = db

    def schedules(self) -> list[dict]:
        return self.db.read("SELECT * FROM schedules ORDER BY time_hhmm,id")

    def save_schedule(self, data: dict, schedule_id: int | None = None) -> int:
        args = (
            data["time_hhmm"],
            data.get("label", ""),
            data.get("window_mode", "previous-day"),
            int(data.get("enabled", True)),
        )
        if schedule_id is None:
            return self.db.write(
                "INSERT INTO schedules(time_hhmm,label,window_mode,enabled,created_at) VALUES(?,?,?,?,?)",
                args + (int(time.time()),),
                True,
            )
        if not self.db.write(
            "UPDATE schedules SET time_hhmm=?,label=?,window_mode=?,enabled=? WHERE id=?",
            args + (schedule_id,),
        ):
            raise EscalationError(404, "Schedule not found")
        return schedule_id

    def delete_schedule(self, schedule_id: int) -> None:
        if not self.db.write("DELETE FROM schedules WHERE id=?", (schedule_id,)):
            raise EscalationError(404, "Schedule not found")

    def presets(self, owner: str) -> list[dict]:
        return [
            {**r, "config": json.loads(r["config"])}
            for r in self.db.read("SELECT * FROM report_presets WHERE owner_id=? ORDER BY id DESC", (owner,))
        ]

    def save_preset(self, owner: str, name: str, config: dict, preset_id: int | None = None) -> int:
        now = int(time.time())
        if preset_id is None:
            return self.db.write(
                "INSERT INTO report_presets(owner_id,name,config,created_at,updated_at) VALUES(?,?,?,?,?)",
                (owner, name, json.dumps(config), now, now),
                True,
            )
        if not self.db.write(
            "UPDATE report_presets SET name=?,config=?,updated_at=? WHERE id=? AND owner_id=?",
            (name, json.dumps(config), now, preset_id, owner),
        ):
            raise EscalationError(404, "Preset not found")
        return preset_id

    def delete_preset(self, owner: str, preset_id: int) -> None:
        if not self.db.write("DELETE FROM report_presets WHERE id=? AND owner_id=?", (preset_id, owner)):
            raise EscalationError(404, "Preset not found")
