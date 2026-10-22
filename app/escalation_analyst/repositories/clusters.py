"""Cluster directory and membership persistence."""

from __future__ import annotations

import time
from ..errors import EscalationError


class Clusters:
    def __init__(self, db):
        self.db = db

    def list(self) -> list[dict]:
        rows = self.db.read("SELECT * FROM clusters ORDER BY created_at,id")
        for row in rows:
            sql = (
                "SELECT COUNT(*) AS n FROM whatsapp_groups"
                if row["id"] == "all"
                else "SELECT COUNT(*) AS n FROM group_clusters WHERE cluster_id=?"
            )
            row["group_count"] = self.db.read(sql, () if row["id"] == "all" else (row["id"],))[0]["n"]
        return rows

    def get(self, cluster_id: str, active: bool = False) -> dict:
        rows = self.db.read("SELECT * FROM clusters WHERE id=?", (cluster_id,))
        if not rows:
            raise EscalationError(404, "Cluster not found")
        if active and not rows[0]["is_active"]:
            raise EscalationError(400, "Cluster is inactive")
        return rows[0]

    def create(self, cluster_id: str, name: str) -> None:
        if cluster_id == "all":
            raise EscalationError(400, "The all cluster is protected")
        count = self.db.write(
            "INSERT INTO clusters(id,name,created_at) VALUES(?,?,?) ON CONFLICT(id) DO NOTHING",
            (cluster_id, name, int(time.time())),
        )
        if not count:
            raise EscalationError(409, "Cluster ID already exists")

    def update(self, cluster_id: str, name: str | None = None, status: bool | None = None) -> dict:
        self.get(cluster_id)
        if cluster_id == "all":
            raise EscalationError(400, "The all cluster is protected")
        if name is not None:
            self.db.write("UPDATE clusters SET name=? WHERE id=?", (name, cluster_id))
        if status is not None:
            self.db.write("UPDATE clusters SET is_active=? WHERE id=?", (int(status), cluster_id))
        return self.get(cluster_id)

    def assign(self, group_id: str, cluster_id: str, name: str = "") -> None:
        self.get(cluster_id, active=True)
        self.db.write(
            "INSERT INTO group_clusters(group_id,cluster_id,group_name,updated_at) VALUES(?,?,?,?) ON CONFLICT(group_id) DO UPDATE SET cluster_id=excluded.cluster_id,group_name=excluded.group_name,updated_at=excluded.updated_at",
            (group_id, cluster_id, name, int(time.time())),
        )

    def map(self) -> dict:
        return {
            r["group_id"]: r["cluster_id"]
            for r in self.db.read("SELECT group_id,cluster_id FROM group_clusters")
        }
