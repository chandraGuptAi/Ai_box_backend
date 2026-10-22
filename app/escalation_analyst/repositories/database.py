"""Connection boundary for separate SQLite storage or a private PostgreSQL schema."""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import threading
import time
from pathlib import Path
from .schema import DDL


class Database:
    def __init__(self, settings):
        self.settings = settings
        self.postgres = bool(settings.database_url)
        self._ready = False
        self._lock = threading.Lock()

    @contextmanager
    def connection(self):
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row

            with psycopg.connect(
                self.settings.database_url, row_factory=dict_row, connect_timeout=10
            ) as conn:
                conn.execute("CREATE SCHEMA IF NOT EXISTS escalation_analyst")
                conn.execute("SET search_path TO escalation_analyst")
                yield conn
        else:
            Path(self.settings.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.settings.sqlite_path, timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA journal_mode=WAL")
                with conn:
                    yield conn
            finally:
                conn.close()

    def sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.postgres else sql

    def initialize(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            with self.connection() as conn:
                for statement in DDL.split(";"):
                    if statement.strip():
                        conn.execute(
                            statement.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
                            if self.postgres
                            else statement
                        )
                conn.execute(
                    self.sql(
                        "INSERT INTO clusters(id,name,created_at) VALUES(?,?,?) ON CONFLICT(id) DO NOTHING"
                    ),
                    ("all", "All", int(time.time())),
                )
                conn.execute("INSERT INTO schema_version(version) VALUES(1) ON CONFLICT(version) DO NOTHING")
                conn.execute(
                    "INSERT INTO group_activity_policy(id,days,enabled) VALUES(1,7,1) ON CONFLICT(id) DO NOTHING"
                )
            self._ready = True

    def read(self, sql: str, args: tuple = ()) -> list[dict]:
        self.initialize()
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(self.sql(sql), args).fetchall()]

    def write(self, sql: str, args: tuple = (), returning: bool = False):
        self.initialize()
        with self.connection() as conn:
            cursor = conn.execute(self.sql(sql + (" RETURNING id" if returning else "")), args)
            return cursor.fetchone()["id"] if returning else cursor.rowcount
