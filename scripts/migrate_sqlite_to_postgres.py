"""Copy legacy reports without overwriting existing PostgreSQL records.

Run from the project root: python -m scripts.migrate_sqlite_to_postgres
"""

import sqlite3
from contextlib import closing
from pathlib import Path

from psycopg import sql

from app.config import BASE_DIR
from app.storage.database import storage


def migrate(source: Path = BASE_DIR / "data" / "ai_box.db") -> int:
    if not source.exists():
        print("No legacy SQLite database found; nothing to migrate.")
        return 0
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as legacy:
        legacy.row_factory = sqlite3.Row
        rows = legacy.execute("SELECT * FROM saved_reports").fetchall()
    copied = 0
    with storage._connection() as conn:
        for row in rows:
            columns = list(row.keys())
            query = sql.SQL(
                "INSERT INTO saved_reports ({}) VALUES ({}) ON CONFLICT (id) DO NOTHING"
            ).format(
                sql.SQL(", ").join(map(sql.Identifier, columns)),
                sql.SQL(", ").join(sql.Placeholder() for _ in columns),
            )
            copied += conn.execute(query, tuple(row)).rowcount
    print(f"Copied {copied} reports to PostgreSQL; SQLite source preserved.")
    return copied


if __name__ == "__main__":
    migrate()
