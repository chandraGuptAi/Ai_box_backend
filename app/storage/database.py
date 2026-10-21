"""Database storage and analytics engine for AI Box reports.

Uses PostgreSQL for persistent report storage.
Stores complete report documents, rendered PDF/DOCX files, and computes
real-time dashboard analytics.
"""

from __future__ import annotations

import json
import logging
import os
import psycopg
from psycopg.rows import dict_row

from app.config import BASE_DIR  # Ensures the local .env is loaded before storage starts.
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

logger = logging.getLogger("ai_box.storage")

_OUTPUT_DIR = BASE_DIR / "output" / "reports"


class ReportStorage:
    def __init__(self, database_url: str | None = None):
        self._url = database_url or os.getenv("DATABASE_URL", "").strip()
        if not self._url:
            raise ValueError("DATABASE_URL is required. Start PostgreSQL with docker compose up -d postgres.")
        if not self._url.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL must use postgresql:// or postgres://.")
        self._init_db()

    @contextmanager
    def _connection(self) -> Generator[psycopg.Connection, None, None]:
        with psycopg.connect(self._url, row_factory=dict_row, connect_timeout=10) as conn:
            yield conn

    def _init_db(self) -> None:
        table_sql = """
        CREATE TABLE IF NOT EXISTS saved_reports (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            report_type TEXT NOT NULL,
            prompt TEXT DEFAULT '',
            records_analyzed INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            created_by_id TEXT DEFAULT 'user',
            created_by_name TEXT DEFAULT 'User',
            report_json TEXT NOT NULL,
            pdf_path TEXT DEFAULT '',
            docx_path TEXT DEFAULT '',
            pdf_blob BYTEA,
            docx_blob BYTEA,
            status TEXT DEFAULT 'saved'
        );
        CREATE INDEX IF NOT EXISTS idx_saved_reports_created ON saved_reports(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_saved_reports_type ON saved_reports(report_type);
        CREATE INDEX IF NOT EXISTS idx_saved_reports_owner ON saved_reports(created_by_id, created_at DESC);
        """
        with self._connection() as conn:
            conn.execute(table_sql)
        logger.info("PostgreSQL storage database initialized successfully.")

    def save_report(
        self,
        report_id: str,
        name: str,
        description: str,
        report_type: str,
        prompt: str,
        records_analyzed: int,
        report_data: dict[str, Any],
        *,
        created_by_id: str,
        created_by_name: str,
        pdf_bytes: bytes | None = None,
        docx_bytes: bytes | None = None,
    ) -> dict[str, Any] | None:
        """Save or update a generated report in the database and write files to disk.

        Returns None when the ID already belongs to another user; their report is left untouched.
        """
        with self._connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT created_by_id FROM saved_reports WHERE id = %s", (report_id,))
            existing = cur.fetchone()
        if existing and existing["created_by_id"] != created_by_id:
            return None

        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        pdf_path = ""
        docx_path = ""

        if pdf_bytes:
            pdf_file = _OUTPUT_DIR / f"{report_id}.pdf"
            pdf_file.write_bytes(pdf_bytes)
            pdf_path = str(pdf_file)

        if docx_bytes:
            docx_file = _OUTPUT_DIR / f"{report_id}.docx"
            docx_file.write_bytes(docx_bytes)
            docx_path = str(docx_file)

        now_iso = datetime.now(timezone.utc).isoformat()
        report_json_str = json.dumps(report_data, ensure_ascii=False)

        sql = """
        INSERT INTO saved_reports (
            id, name, description, report_type, prompt, records_analyzed,
            created_at, created_by_id, created_by_name, report_json,
            pdf_path, docx_path, pdf_blob, docx_blob, status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'saved')
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            description = excluded.description,
            report_type = excluded.report_type,
            prompt = excluded.prompt,
            records_analyzed = excluded.records_analyzed,
            report_json = excluded.report_json,
            pdf_path = CASE WHEN excluded.pdf_path != '' THEN excluded.pdf_path ELSE saved_reports.pdf_path END,
            docx_path = CASE WHEN excluded.docx_path != '' THEN excluded.docx_path ELSE saved_reports.docx_path END,
            pdf_blob = CASE WHEN excluded.pdf_blob IS NOT NULL THEN excluded.pdf_blob ELSE saved_reports.pdf_blob END,
            docx_blob = CASE WHEN excluded.docx_blob IS NOT NULL THEN excluded.docx_blob ELSE saved_reports.docx_blob END
        WHERE saved_reports.created_by_id = excluded.created_by_id;
        """

        with self._connection() as conn:
            cur = conn.cursor()
            cur.execute(
                sql,
                (
                    report_id,
                    name,
                    description,
                    report_type,
                    prompt,
                    records_analyzed,
                    now_iso,
                    created_by_id,
                    created_by_name,
                    report_json_str,
                    pdf_path,
                    docx_path,
                    pdf_bytes,
                    docx_bytes,
                ),
            )
            if cur.rowcount == 0:
                return None

        return self.get_report(report_id, owner_id=created_by_id)

    def get_report(self, report_id: str, *, owner_id: str) -> dict[str, Any] | None:
        """Retrieve one of the owner's saved reports by ID with full parsed JSON."""
        sql = "SELECT * FROM saved_reports WHERE id = %s AND created_by_id = %s"
        with self._connection() as conn:
            cur = conn.cursor()
            cur.execute(sql, (report_id, owner_id))
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_dict(row)

    def get_file_content(self, report_id: str, file_type: str = "pdf", *, owner_id: str) -> tuple[bytes | None, str]:
        """Retrieve the binary content of one of the owner's stored PDF or DOCX files."""
        sql = ("SELECT name, pdf_path, docx_path, pdf_blob, docx_blob FROM saved_reports "
               "WHERE id = %s AND created_by_id = %s")
        with self._connection() as conn:
            cur = conn.cursor()
            cur.execute(sql, (report_id, owner_id))
            row = cur.fetchone()
            if not row:
                return None, ""

            name = row["name"]
            safe_name = "".join(c if c.isalnum() or c in " ._-" else "_" for c in name).strip()
            ext = file_type.lower()
            filename = f"{safe_name}.{ext}" if safe_name else f"report_{report_id}.{ext}"

            # 1. Try blob from DB
            blob = row["pdf_blob"] if ext == "pdf" else row["docx_blob"]
            if blob:
                return blob, filename

            # 2. Try file path on disk
            file_path = row["pdf_path"] if ext == "pdf" else row["docx_path"]
            if file_path and os.path.exists(file_path):
                try:
                    return Path(file_path).read_bytes(), filename
                except Exception as exc:
                    logger.warning("Could not read file from disk path %s: %s", file_path, exc)

            return None, filename

    def update_report(self, report_id: str, name: str | None = None, description: str | None = None,
                      *, owner_id: str) -> dict[str, Any] | None:
        """Update the title/name or description of one of the owner's reports."""
        updates = []
        params = []
        if name is not None:
            updates.append("name = %s")
            params.append(name.strip())
        if description is not None:
            updates.append("description = %s")
            params.append(description.strip())
        if not updates:
            return self.get_report(report_id, owner_id=owner_id)

        params.extend([report_id, owner_id])
        sql = f"UPDATE saved_reports SET {', '.join(updates)} WHERE id = %s AND created_by_id = %s"
        with self._connection() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
        return self.get_report(report_id, owner_id=owner_id)

    def delete_report(self, report_id: str, *, owner_id: str) -> bool:
        """Delete one of the owner's report records and cleanup any disk files."""
        record = self.get_report(report_id, owner_id=owner_id)
        if not record:
            return False
        for p in (record.get("pdf_path"), record.get("docx_path")):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

        sql = "DELETE FROM saved_reports WHERE id = %s AND created_by_id = %s"
        with self._connection() as conn:
            cur = conn.cursor()
            cur.execute(sql, (report_id, owner_id))
            return cur.rowcount > 0

    def list_reports(
        self,
        search: str = "",
        report_type: str = "",
        page: int = 1,
        page_size: int = 10,
        *,
        owner_id: str,
    ) -> dict[str, Any]:
        """List the owner's reports with search, filtering, and pagination."""
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        offset = (page - 1) * page_size

        where_clauses = ["created_by_id = %s"]
        params: list[Any] = [owner_id]

        if search:
            where_clauses.append("(LOWER(name) LIKE %s OR LOWER(description) LIKE %s OR LOWER(prompt) LIKE %s)")
            pattern = f"%{search.lower().strip()}%"
            params.extend([pattern, pattern, pattern])

        if report_type:
            where_clauses.append("LOWER(REPLACE(REPLACE(report_type, '-', ''), '_', '')) LIKE %s")
            norm_type = f"%{report_type.lower().replace('-', '').replace('_', '')}%"
            params.append(norm_type)

        where_sql = f"WHERE {' AND '.join(where_clauses)}"

        count_sql = f"SELECT COUNT(*) as total FROM saved_reports {where_sql}"
        select_sql = f"""
        SELECT * FROM saved_reports
        {where_sql}
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
        """

        with self._connection() as conn:
            cur = conn.cursor()
            cur.execute(count_sql, params)
            total = cur.fetchone()["total"]

            cur.execute(select_sql, params + [page_size, offset])
            rows = cur.fetchall()
            items = [self._row_to_dict(row) for row in rows]

        return {
            "items": items,
            "total": total,
            "page": page,
            "pageSize": page_size,
        }

    def get_analytics_overview(self, *, owner_id: str) -> dict[str, Any]:
        """Compute real-time dashboard analytics across the owner's saved reports."""
        now = datetime.now(timezone.utc)
        curr_month = now.strftime("%Y-%m")
        # Previous month
        prev_year = now.year if now.month > 1 else now.year - 1
        prev_month_num = now.month - 1 if now.month > 1 else 12
        prev_month = f"{prev_year:04d}-{prev_month_num:02d}"

        with self._connection() as conn:
            cur = conn.cursor()

            # 1. Total reports
            cur.execute("SELECT COUNT(*) as total, COALESCE(SUM(records_analyzed), 0) as total_records "
                        "FROM saved_reports WHERE created_by_id = %s", (owner_id,))
            row_total = cur.fetchone()
            total_reports = row_total["total"] if row_total else 0
            total_records = row_total["total_records"] if row_total else 0

            # 2. Reports this month
            cur.execute("SELECT COUNT(*) as this_month FROM saved_reports WHERE created_by_id = %s AND created_at LIKE %s",
                        (owner_id, f"{curr_month}%"))
            row_curr = cur.fetchone()
            reports_this_month = row_curr["this_month"] if row_curr else 0

            # 3. Reports last month
            cur.execute("SELECT COUNT(*) as last_month FROM saved_reports WHERE created_by_id = %s AND created_at LIKE %s",
                        (owner_id, f"{prev_month}%"))
            row_prev = cur.fetchone()
            reports_last_month = row_prev["last_month"] if row_prev else 0

            # Monthly change percentage
            monthly_change_pct: float | None = None
            if reports_last_month > 0:
                monthly_change_pct = round(((reports_this_month - reports_last_month) / reports_last_month) * 100, 1)
            elif reports_this_month > 0:
                monthly_change_pct = 100.0

            # 4. Last report
            cur.execute("SELECT id, name, created_at FROM saved_reports WHERE created_by_id = %s "
                        "ORDER BY created_at DESC LIMIT 1", (owner_id,))
            row_last = cur.fetchone()
            last_report = None
            if row_last:
                last_report = {
                    "id": row_last["id"],
                    "name": row_last["name"],
                    "createdAt": row_last["created_at"],
                }

            # 5. Recent reports (formatted for Angular SavedReport / ReportsOverview)
            cur.execute("SELECT * FROM saved_reports WHERE created_by_id = %s ORDER BY created_at DESC LIMIT 5",
                        (owner_id,))
            recent_rows = cur.fetchall()
            recent_reports = [self._to_angular_saved_report(self._row_to_dict(r)) for r in recent_rows]

        return {
            "totalReports": total_reports,
            "reportsThisMonth": reports_this_month,
            "monthlyChangePercent": monthly_change_pct,
            "availableRecords": total_records,
            "lastReport": last_report,
            "recentReports": recent_reports,
            "source": "live",
        }

    def _row_to_dict(self, row: dict[str, Any]) -> dict[str, Any]:
        """Convert a database row into a structured dictionary."""
        d = dict(row)
        # Parse report_json safely
        report_obj = {}
        if d.get("report_json"):
            try:
                report_obj = json.loads(d["report_json"])
            except Exception:
                report_obj = {}
        d["report"] = report_obj
        # Don't send heavy binary blobs over standard JSON API
        d.pop("pdf_blob", None)
        d.pop("docx_blob", None)
        return d

    def _to_angular_saved_report(self, record: dict[str, Any]) -> dict[str, Any]:
        """Map a saved report dictionary to Angular SavedReport structure."""
        report = record.get("report", {})
        created_at = record.get("created_at", datetime.now(timezone.utc).isoformat())

        # Dates extraction
        from_date = created_at
        to_date = created_at
        metrics = report.get("key_metrics", [])
        for m in metrics:
            if isinstance(m, dict) and "Date Range" in m.get("label", ""):
                parts = str(m.get("value", "")).split("–")
                if len(parts) == 2:
                    from_date = parts[0].strip()
                    to_date = parts[1].strip()

        # KPIs extraction
        kpis = []
        structured = report.get("structured_report", {})
        for idx, m in enumerate(structured.get("metrics", []) or metrics):
            if isinstance(m, dict):
                kpis.append({
                    "id": str(idx),
                    "label": m.get("label", "Metric"),
                    "value": float(m.get("value", 0)) if str(m.get("value", "")).replace(".", "", 1).isdigit() else 0,
                    "format": "number",
                })

        # Charts extraction
        charts = []
        for c in structured.get("charts", []):
            if isinstance(c, dict):
                charts.append({
                    "id": c.get("id", "chart"),
                    "title": c.get("title", "Chart"),
                    "type": c.get("type", "bar"),
                    "labels": c.get("labels", []),
                    "series": [{"name": ds.get("label", "Value"), "values": ds.get("data", [])} for ds in c.get("datasets", [])],
                })

        return {
            "id": record.get("id"),
            "name": record.get("name"),
            "description": record.get("description", ""),
            "createdAt": created_at,
            "createdBy": {
                "id": record.get("created_by_id", "user"),
                "name": record.get("created_by_name", "User"),
            },
            "result": {
                "id": record.get("id"),
                "request": {
                    "prompt": record.get("prompt", ""),
                    "filters": {
                        "reportType": record.get("report_type", "shipments"),
                        "dateRange": "custom",
                        "status": "All",
                        "fromDate": from_date,
                        "toDate": to_date,
                    },
                },
                "createdAt": created_at,
                "source": "live",
                "kpis": kpis,
                "charts": charts,
                "insights": [{"id": str(i), "title": f.get("title", ""), "text": f.get("detail", ""), "tone": "neutral"} for i, f in enumerate(report.get("content", {}).get("findings", []))],
                "data": {
                    "columns": [],
                    "rows": [],
                    "page": 1,
                    "pageSize": 10,
                    "total": record.get("records_analyzed", 0),
                },
                "title": record.get("name"),
                "recordCount": record.get("records_analyzed", 0),
            },
            "report": report,
        }


# Global storage instance
storage = ReportStorage()
