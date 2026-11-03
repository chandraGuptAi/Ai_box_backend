"""Formatted admin audit workbook export."""
from io import BytesIO
from datetime import datetime, timezone, timedelta
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from app.services.audit_trail import CSV_COLUMNS, summarize


def build_audit_excel(items, start, end, filters):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Report Audit"
    sheet.sheet_view.showGridLines = False
    columns = list(CSV_COLUMNS)
    count = len(columns)

    def banner(row, text, color, size=11):
        sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=count)
        cell = sheet.cell(row, 1, text)
        cell.font = Font(name="Calibri", size=size, bold=True, color="FFFFFF")
        cell.alignment = Alignment(vertical="center")
        for cell in sheet[row]:
            cell.fill = PatternFill("solid", fgColor=color)
        sheet.row_dimensions[row].height = 30

    banner(1, "BIGSHIP | AI Box Report Audit", "111B53", 18)
    stamp = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    banner(2, f"Period: {start} to {end} (IST) | Exported: {stamp:%d %b %Y, %I:%M %p} IST", "3346A8")
    active = "; ".join(f"{k.replace('_', ' ').title()}: {v}" for k, v in filters.items() if v)
    sheet.cell(3, 1, "Filters: " + (active or "All requests in selected period"))
    sheet.merge_cells(start_row=3, start_column=1, end_row=3, end_column=count)
    sheet.cell(3, 1).alignment = Alignment(wrap_text=True)
    sheet.row_dimensions[3].height = 30
    summary = summarize(items)
    for col, (label, value) in zip((1, 4, 7, 10, 13), (
        ("TOTAL REQUESTS", summary["requests"]), ("GENERATED", summary["generated"]),
        ("FAILED", summary["failed"]), ("SUCCESS RATE", summary["success_rate"]),
        ("UNIQUE USERS", summary["unique_users"]))):
        sheet.cell(5, col, label).font = Font(bold=True, color="64748B", size=10)
        cell = sheet.cell(6, col, value / 100 if label == "SUCCESS RATE" and value is not None else value)
        cell.font = Font(bold=True, size=20, color="111B53")
        if label == "SUCCESS RATE":
            cell.number_format = "0.0%"
    headers = [key.replace("_", " ").title().replace("Ms", "(ms)").replace("Ist", "IST") for key in columns]
    for col, label in enumerate(headers, 1):
        cell = sheet.cell(8, col, label)
        cell.fill = PatternFill("solid", fgColor="3346A8")
        cell.font = Font(bold=True, color="FFFFFF")
        cell.alignment = Alignment(vertical="center")
        key = columns[col - 1]
        sheet.column_dimensions[get_column_letter(col)].width = 65 if key == "prompt" else 30 if key in ("company_name", "error_code", "request_id", "report_id") else 20
    sheet.row_dimensions[8].height = 30
    for index, item in enumerate(items, 9):
        for col, key in enumerate(columns, 1):
            value = item.get(key)
            cell = sheet.cell(index, col, value)
            if isinstance(value, str):
                cell.data_type = "s"  # Treat user-supplied prompts as text, never Excel formulas.
            cell.font = Font(name="Calibri", size=11, color="1E293B")
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if index % 2:
                cell.fill = PatternFill("solid", fgColor="F1F5FB")
            if key == "outcome":
                cell.font = Font(bold=True, color="15803D" if value == "generated" else "B91C1C")
            if key in ("duration_ms", "records_analyzed", "shipments_matched", "ai_calls"):
                cell.number_format = "#,##0"
        sheet.row_dimensions[index].height = 48
    sheet.freeze_panes = "D9"
    sheet.auto_filter.ref = f"A8:{get_column_letter(count)}{max(8, sheet.max_row)}"
    sheet.print_title_rows = "1:8"
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A3
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
