"""Combine saved AI report tables into a single safe Excel workbook."""

from __future__ import annotations
import io
from openpyxl import Workbook
from .documents import tables
from ..logistics.workbook import safe, style_sheet
from ..errors import EscalationError


def export_bulk(reports: list[dict]) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "Escalations"
    header = None
    for report in reports:
        found = tables(report["report"])
        if not found:
            continue
        table = found[0]
        if header is None:
            header = ["Company", *table[0]]
            sheet.append(header)
        if table[0] != header[1:]:
            raise EscalationError(400, "Saved reports have incompatible table columns")
        for row in table[1:]:
            sheet.append([safe(v) for v in [report.get("group_name") or report["group_id"], *row]])
    if header is None:
        raise EscalationError(404, "No escalation tables available for export")
    style_sheet(sheet, 1, [30] * len(header))
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()
