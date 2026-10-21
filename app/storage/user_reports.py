"""One user's view of the saved reports.

Routes work with ``UserReports`` instead of ``ReportStorage`` directly. The owner is
fixed when the object is created, so no call can read or change another user's rows.
"""

from __future__ import annotations

from typing import Any

from app.storage.database import ReportStorage


class UserReports:
    def __init__(self, store: ReportStorage, user_id: str, user_name: str):
        self._store = store
        self.user_id = user_id
        self.user_name = user_name

    def get(self, report_id: str) -> dict[str, Any] | None:
        return self._store.get_report(report_id, owner_id=self.user_id)

    def file(self, report_id: str, file_type: str) -> tuple[bytes | None, str]:
        record = self.get(report_id)
        if record and isinstance(record.get("report"), dict) and record["report"].get("report_type") == "shipment":
            from app.models.report_schema import ReportDocument
            from app.analysis.accuracy import verify_revision
            verify_revision(ReportDocument.model_validate(record["report"]))
        return self._store.get_file_content(report_id, file_type, owner_id=self.user_id)

    def list(self, search: str = "", report_type: str = "", page: int = 1, page_size: int = 10) -> dict[str, Any]:
        return self._store.list_reports(search=search, report_type=report_type, page=page,
                                        page_size=page_size, owner_id=self.user_id)

    def save(self, *, report_id: str, name: str, description: str, report_type: str, prompt: str,
             records_analyzed: int, report_data: dict[str, Any],
             pdf_bytes: bytes | None = None, docx_bytes: bytes | None = None) -> dict[str, Any] | None:
        """None when the report ID belongs to another user."""
        return self._store.save_report(
            report_id=report_id, name=name, description=description, report_type=report_type,
            prompt=prompt, records_analyzed=records_analyzed, report_data=report_data,
            created_by_id=self.user_id, created_by_name=self.user_name,
            pdf_bytes=pdf_bytes, docx_bytes=docx_bytes,
        )

    def rename(self, report_id: str, name: str | None = None, description: str | None = None) -> dict[str, Any] | None:
        return self._store.update_report(report_id, name=name, description=description, owner_id=self.user_id)

    def delete(self, report_id: str) -> bool:
        return self._store.delete_report(report_id, owner_id=self.user_id)

    def overview(self) -> dict[str, Any]:
        return self._store.get_analytics_overview(owner_id=self.user_id)

    def to_angular(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._store._to_angular_saved_report(record)
