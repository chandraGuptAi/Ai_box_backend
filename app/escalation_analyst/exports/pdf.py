"""Small native PDF export using the host's existing ReportLab dependency."""

from __future__ import annotations
import io
from xml.sax.saxutils import escape
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, LongTable, TableStyle


def export_pdf(details: dict, rows: list[dict]) -> bytes:
    output = io.BytesIO()
    styles = getSampleStyleSheet()
    document = SimpleDocTemplate(
        output, pagesize=landscape(A4), leftMargin=28, rightMargin=28, topMargin=36, bottomMargin=36
    )
    story = [Paragraph("Bigship Escalation Performance", styles["Title"])]
    for line in details["summary"]:
        story.append(Paragraph(escape(line), styles["BodyText"]))
    story.append(Spacer(1, 16))
    data = [["AWB", "Group", "Observed status", "ERP status", "Linked reply"]]
    for row in rows:
        data.append(
            [
                Paragraph(escape(str(value if value is not None else "Not available")), styles["BodyText"])
                for value in (
                    row["awb"],
                    row["groupName"],
                    row["observed"],
                    row["erpStatus"],
                    row["linkedReplySeconds"],
                )
            ]
        )
    table = LongTable(data, colWidths=[95, 190, 150, 150, 100], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#087f6a")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(table)

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.drawString(28, 18, f"Bigship Escalation Analyst / page {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()
