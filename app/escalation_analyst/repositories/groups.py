"""Group names, assignments and captured-message queries."""

from __future__ import annotations

import time
from ..errors import EscalationError
from ..utils.ist import day, midnight


class Groups:
    def __init__(self, db):
        self.db = db

    def list(self, window: dict | None = None) -> list[dict]:
        start = midnight(day(time.time()))
        rows = self.db.read(
            """SELECT g.group_id,
            COALESCE(o.name,l.name,g.group_name,g.group_id) AS group_name,g.is_active,
            COUNT(m.id) AS total_messages, COALESCE(SUM(CASE WHEN m.timestamp>=? THEN 1 ELSE 0 END),0) AS today_messages,
            MAX(m.timestamp) AS last_message_ts
            FROM whatsapp_groups g LEFT JOIN messages m ON m.group_id=g.group_id
            LEFT JOIN group_name_overrides o ON o.group_id=g.group_id
            LEFT JOIN group_names_learned l ON l.group_id=g.group_id
            GROUP BY g.group_id,g.group_name,g.is_active,o.name,l.name
            ORDER BY last_message_ts DESC,g.group_id""",
            (start,),
        )
        policy = self.policy()
        for row in rows:
            inactive = (
                policy["enabled"] and (row["last_message_ts"] or 0) < time.time() - policy["days"] * 86400
            )
            row.update(activity_status="inactive" if inactive else "active", inactivity_days=policy["days"])
            if window:
                items = self.messages(row["group_id"], window)
                row.update(
                    date_messages=len(items),
                    date_last_message_ts=max((m["timestamp"] for m in items), default=None),
                )
        return rows

    def get(self, group_id: str) -> dict:
        group = next((g for g in self.list() if g["group_id"] == group_id), None)
        if not group:
            raise EscalationError(404, "Group not found")
        return group

    def learn(self, group_id: str, name: str) -> None:
        self.db.write(
            "INSERT INTO whatsapp_groups(group_id,group_name) VALUES(?,?) ON CONFLICT(group_id) DO UPDATE SET group_name=excluded.group_name",
            (group_id, name),
        )
        if name and name != group_id:
            self.db.write(
                "INSERT INTO group_names_learned(group_id,name) VALUES(?,?) ON CONFLICT(group_id) DO UPDATE SET name=excluded.name",
                (group_id, name),
            )

    def name(self, group_id: str, name: str) -> None:
        self.get(group_id)
        self.db.write(
            "INSERT INTO group_name_overrides(group_id,name) VALUES(?,?) ON CONFLICT(group_id) DO UPDATE SET name=excluded.name",
            (group_id, name),
        )

    def status(self, group_id: str, active: bool) -> dict:
        self.get(group_id)
        self.db.write("UPDATE whatsapp_groups SET is_active=? WHERE group_id=?", (int(active), group_id))
        return self.get(group_id)

    def ingest(self, data: dict) -> bool:
        self.learn(data["group_id"], data.get("group_name") or data["group_id"])
        active = self.db.read("SELECT is_active FROM whatsapp_groups WHERE group_id=?", (data["group_id"],))[
            0
        ]["is_active"]
        if not active:
            return False
        keys = ("wa_id", "group_id", "group_name", "author", "author_name", "body", "msg_type", "timestamp")
        return bool(
            self.db.write(
                "INSERT INTO messages(wa_id,group_id,group_name,author,author_name,body,msg_type,timestamp,has_media,created_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(wa_id) WHERE wa_id IS NOT NULL DO NOTHING",
                tuple(data.get(k) for k in keys)
                + (int(bool(data.get("media") or data.get("has_media"))), int(time.time())),
            )
        )

    def messages(
        self, group_id: str | None, window: dict | None = None, group_ids: list | None = None
    ) -> list[dict]:
        conditions, args = [], []
        if group_id:
            conditions.append("group_id=?")
            args.append(group_id)
        if group_ids is not None:
            if not group_ids:
                return []
            conditions.append("group_id IN (" + ",".join("?" for _ in group_ids) + ")")
            args.extend(group_ids)
        if window:
            conditions.append("timestamp>=? AND timestamp" + ("<=?" if window.get("inclusive") else "<?"))
            args.extend((window["from"], window["to"]))
        return self.db.read(
            "SELECT * FROM messages"
            + (" WHERE " + " AND ".join(conditions) if conditions else "")
            + " ORDER BY timestamp,id",
            tuple(args),
        )

    def delete(self, group_id: str) -> dict:
        self.get(group_id)
        self.db.initialize()
        with self.db.connection() as conn:
            counts = {
                table: conn.execute(
                    self.db.sql(f"DELETE FROM {table} WHERE group_id=?"), (group_id,)
                ).rowcount
                for table in ("messages", "reports")
            }
            for table in ("group_clusters", "group_name_overrides", "group_names_learned", "whatsapp_groups"):
                conn.execute(self.db.sql(f"DELETE FROM {table} WHERE group_id=?"), (group_id,))
        return counts

    def policy(self) -> dict:
        row = self.db.read("SELECT days,enabled FROM group_activity_policy WHERE id=1")[0]
        return {**row, "enabled": bool(row["enabled"]), "source": "settings"}

    def set_policy(self, days: int, enabled: bool) -> dict:
        self.db.write("UPDATE group_activity_policy SET days=?,enabled=? WHERE id=1", (days, int(enabled)))
        return self.policy()

    def purge(self, days: int) -> int:
        return (
            self.db.write("DELETE FROM messages WHERE timestamp<?", (int(time.time()) - days * 86400,))
            if days > 0
            else 0
        )

    def transport_info(self, group_id: str) -> dict:
        groups = self.db.read("SELECT * FROM whatsapp_groups WHERE group_id=?", (group_id,))
        override = self.db.read("SELECT name FROM group_name_overrides WHERE group_id=?", (group_id,))
        learned = self.db.read("SELECT name FROM group_names_learned WHERE group_id=?", (group_id,))
        last = self.db.read("SELECT MAX(timestamp) AS ts FROM messages WHERE group_id=?", (group_id,))[0][
            "ts"
        ]
        return dict(
            is_active=groups[0]["is_active"] if groups else 1,
            name=(
                override[0]["name"]
                if override
                else learned[0]["name"] if learned else groups[0]["group_name"] if groups else group_id
            ),
            override=override[0]["name"] if override else None,
            learned=learned[0]["name"] if learned else None,
            last_message_ts=last,
        )

    def ingest_info(self, data: dict) -> dict:
        inserted = self.ingest(data)
        rows = self.db.read("SELECT id FROM messages WHERE wa_id=?", (data["wa_id"],))
        return {"id": rows[0]["id"] if rows else None, "inserted": inserted}

    def update_body(self, message_id: int, body: str) -> None:
        self.db.write("UPDATE messages SET body=? WHERE id=?", (body, message_id))
