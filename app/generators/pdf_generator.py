"""PDF report generation with ReportLab - styled identically to the frontend AI Box report."""

from __future__ import annotations

import os
import re
import threading
from io import BytesIO
from types import SimpleNamespace
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT, TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    Image,
    KeepTogether,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.config import settings
from app.models.report_schema import ReportDocument
from app.charts.render import FALLBACK_NOTE, chart_table, render_png

# Color scheme matching Bigship AI Box UI palette
PRIMARY = colors.HexColor("#1E293B")       # Slate 800 - Deep crisp navy for headings
BRAND = colors.HexColor("#4F46E5")         # Indigo 600 - Primary accent
BRAND_LIGHT = colors.HexColor("#EEF0FC")   # Lavender surface
MUTED = colors.HexColor("#64748B")         # Slate 500 - Subtitles & labels
GRID = colors.HexColor("#E2E8F0")          # Slate 200 - Clean subtle borders
CARD_BG = colors.HexColor("#F8FAFC")       # Slate 50 - Card background
ZEBRA = colors.HexColor("#F8FAFC")         # Alternate row background
TEXT_DARK = colors.HexColor("#0F172A")     # Slate 900 - Body text

SEVERITY_COLORS = {
    "high": colors.HexColor("#DC2626"),     # Red
    "critical": colors.HexColor("#DC2626"),
    "bad": colors.HexColor("#DC2626"),
    "medium": colors.HexColor("#D97706"),   # Amber
    "warning": colors.HexColor("#D97706"),
    "low": colors.HexColor("#16A34A"),      # Green
    "ok": colors.HexColor("#16A34A"),
    "info": colors.HexColor("#2563EB"),     # Blue
}

_font_lock = threading.Lock()
_fonts: tuple[str, str] | None = None


def _register_fonts() -> tuple[str, str]:
    """Use a Unicode TrueType font when available (needed for currency & non-Latin text)."""
    global _fonts
    with _font_lock:
        if _fonts is not None:
            return _fonts
        windows_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        candidates = [
            (settings.pdf_font_regular, settings.pdf_font_bold),
            (str(windows_fonts / "arial.ttf"), str(windows_fonts / "arialbd.ttf")),
            (str(windows_fonts / "segoeui.ttf"), str(windows_fonts / "segoeuib.ttf")),
            ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
            ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"),
        ]
        _fonts = ("Helvetica", "Helvetica-Bold")
        for regular, bold in candidates:
            if regular and bold and Path(regular).is_file() and Path(bold).is_file():
                try:
                    pdfmetrics.registerFont(TTFont("AIBox", regular))
                    pdfmetrics.registerFont(TTFont("AIBox-Bold", bold))
                    pdfmetrics.registerFontFamily(
                        "AIBox", normal="AIBox", bold="AIBox-Bold", italic="AIBox", boldItalic="AIBox-Bold"
                    )
                    _fonts = ("AIBox", "AIBox-Bold")
                    break
                except Exception:
                    continue
        return _fonts


def _styles(regular: str, bold: str) -> dict[str, ParagraphStyle]:
    base = ParagraphStyle("Body", fontName=regular, fontSize=9.5, leading=14, spaceAfter=5, textColor=TEXT_DARK)
    return {
        "body": base,
        "badge": ParagraphStyle("Badge", parent=base, fontName=bold, fontSize=8, leading=10, textColor=BRAND, spaceAfter=2),
        "title": ParagraphStyle("Title", parent=base, fontName=bold, fontSize=18, leading=22, textColor=PRIMARY, spaceAfter=4),
        "subtitle": ParagraphStyle("Subtitle", parent=base, fontName=regular, fontSize=10.5, leading=14, textColor=MUTED, spaceAfter=6),
        "meta": ParagraphStyle("Meta", parent=base, fontSize=8, leading=11, textColor=MUTED, spaceAfter=0),
        "h1": ParagraphStyle("H1", parent=base, fontName=bold, fontSize=12.5, leading=16, textColor=PRIMARY, spaceBefore=12, spaceAfter=6, keepWithNext=1),
        "h2": ParagraphStyle("H2", parent=base, fontName=bold, fontSize=10.5, leading=14, textColor=PRIMARY, spaceBefore=8, spaceAfter=4, keepWithNext=1),
        "summary_h": ParagraphStyle("SummaryH", parent=base, fontName=bold, fontSize=11, leading=14, textColor=PRIMARY, spaceAfter=6),
        "bullet": ParagraphStyle("Bullet", parent=base, fontSize=9, leading=13, spaceAfter=3),
        "kpi_label": ParagraphStyle("KPILabel", parent=base, fontName=bold, fontSize=7.5, leading=10, textColor=MUTED),
        "kpi_val": ParagraphStyle("KPIVal", parent=base, fontName=bold, fontSize=14, leading=18, textColor=PRIMARY),
        "kpi_note": ParagraphStyle("KPINote", parent=base, fontSize=7, leading=9, textColor=MUTED),
        "cell": ParagraphStyle("Cell", parent=base, fontSize=8, leading=11, spaceAfter=0),
        "cell_right": ParagraphStyle("CellRight", parent=base, fontSize=8, leading=11, spaceAfter=0, alignment=TA_RIGHT),
        "cell_head": ParagraphStyle("CellHead", parent=base, fontName=bold, fontSize=8, leading=11, spaceAfter=0, textColor=colors.white),
        "indent": ParagraphStyle("Indent", parent=base, leftIndent=0, spaceAfter=6),
        "disclaimer": ParagraphStyle("Disclaimer", parent=base, fontSize=7.5, leading=10.5, textColor=MUTED, spaceBefore=14),
    }


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(text), style)


def _paragraphs(text: str, styles: dict[str, ParagraphStyle]) -> list:
    flowables: list = []
    for block in (b.strip() for b in text.replace("\r\n", "\n").split("\n\n")):
        if not block:
            continue
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if lines and all(line.startswith(("- ", "* ", "• ")) for line in lines):
            flowables.append(ListFlowable(
                [ListItem(_p(line[2:].strip(), styles["body"]), leftIndent=12) for line in lines],
                bulletType="bullet", start="•", leftIndent=12,
            ))
        else:
            flowables.append(_p(" ".join(lines), styles["body"]))
    return flowables


def _is_numeric(value: str) -> bool:
    stripped = value.replace(",", "").replace(".", "", 1).replace("%", "").replace("Rs", "").replace("₹", "").replace("$", "").strip()
    return stripped.isdigit()


def _format_kpi_value(label: str, value: any) -> str:
    val_str = str(value).strip()
    if not val_str:
        return "0"
    if any(sym in val_str for sym in ["₹", "Rs", "$", "%"]):
        return val_str
    if _is_numeric(val_str):
        try:
            f_val = float(val_str.replace(",", ""))
            lbl = label.lower()
            if any(term in lbl for term in ["charge", "shipping", "freight", "cost", "spend", "revenue", "amount"]):
                return f"Rs {f_val:,.2f}"
            if f_val.is_integer():
                return f"{int(f_val):,}"
            return f"{f_val:,.2f}"
        except Exception:
            return val_str
    return val_str


def _table(
    columns: list[str],
    rows: list[list[str]],
    width: float,
    styles: dict[str, ParagraphStyle],
    bold: str,
    align_numbers: bool = True,
) -> Table:
    # Dynamic column widths based on column names and content
    weights = []
    for i, c in enumerate(columns):
        max_len = len(c)
        for r in rows[:12]:
            if i < len(r):
                max_len = max(max_len, len(str(r[i])))
        if i == 0:
            weights.append(max(2.2, max_len / 7.0))
        else:
            weights.append(max(1.0, max_len / 11.0))
    total_w = sum(weights)
    col_widths = [width * (w / total_w) for w in weights]

    data = [[_p(c, styles["cell_head"]) for c in columns]]
    for row in rows:
        data.append([
            _p(str(value), styles["cell_right"] if align_numbers and i > 0 and _is_numeric(str(value)) else styles["cell"])
            for i, value in enumerate(row)
        ])

    table = Table(data, colWidths=col_widths, repeatRows=1, hAlign="LEFT")
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
        ("FONTNAME", (0, 0), (-1, 0), bold),
        ("GRID", (0, 0), (-1, -1), 0.4, GRID),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]
    if len(data) > 1:
        # Prevent orphaning: Header row and first data row(s) must NEVER separate across pages
        commands.append(("NOSPLIT", (0, 0), (-1, min(2, len(data) - 1))))
    for index in range(2, len(data), 2):
        commands.append(("BACKGROUND", (0, index), (-1, index), ZEBRA))
    table.setStyle(TableStyle(commands))
    return table


def _resolve_export_charts(charts, prompt: str | None):
    """Synchronize export chart forms with prompt preferences identically to frontend chartPreference."""
    if not prompt or not charts:
        return charts
    if any(getattr(c, "source", None) for c in charts):
        return charts  # chart-engine charts already have exactly the requested types
    from app.prompts.report_intent import extract_chart_specs
    text = prompt.lower()
    specs = extract_chart_specs(text)
    if specs:
        return charts
    wants_area = bool(re.search(r"\b(?:area[- ](?:charts?|graphs?|plots?)|area\s+(?:charts?|graphs?)|(?:in|as|with)\s+area(?:[- ](?:charts?|graphs?))?)\b", text)) and not re.search(r"\b(no|without)\s+area", text)
    if wants_area:
        resolved = []
        for c in charts:
            c_copy = c.model_copy() if hasattr(c, "model_copy") else c
            if len(c_copy.labels) >= 2:
                c_copy.type = "area"
            resolved.append(c_copy)
        return resolved
    wants_bar = bool(re.search(r"\b(bar|column|baar)\s*(?:charts?|graphs?|cha?rt)?\b", text)) and not re.search(r"\b(no|without)\s+(bar|column)", text)
    both_circular = bool(re.search(r"\b(?:dono\s+(?:graph|chart)[\w\s]*(?:pie|donut|doughnut|circle)|both\s+(?:graphs?|charts?)\s+(?:pie|donut|doughnut|circular)|dono\s+(?:me\s+)?(?:pie|donut))\b", text))
    if wants_bar and not both_circular:
        return charts
    wants_donut = bool(re.search(r"\b(donut|doughnut)s?\b", text)) and not re.search(r"\b(no|without)\s+(donut|doughnut)", text)
    wants_pie = bool(re.search(r"\bpie\b", text)) and not re.search(r"\b(no|without)\s+pie", text)
    if not (wants_donut or wants_pie):
        return charts
    target = "doughnut" if wants_donut else "pie"
    resolved = []
    for c in charts:
        c_copy = c.model_copy() if hasattr(c, "model_copy") else c
        if (c_copy.type or "").lower() == "bar" and len(c_copy.datasets) == 1 and 2 <= len(c_copy.labels) <= 8:
            c_copy.type = target
        resolved.append(c_copy)
    return resolved


def generate_pdf(report: ReportDocument) -> bytes:
    regular, bold = _register_fonts()
    styles = _styles(regular, bold)
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=18 * mm,
        bottomMargin=16 * mm,
        title=report.content.title if report.content else report.report_name,
        author="AI Report",
        subject=report.report_name,
    )
    width = doc.width
    content = report.content
    structured = report.structured_report
    if structured and content:
        content = content.model_copy(update={
            "title": structured.title,
            "findings": structured.findings,
            "recommendations": structured.recommendations
        })

    story: list = []

    # 1. Title Block
    title_text = structured.title if structured and structured.title else (content.title if content else report.report_name)
    story.append(_p(title_text, styles["title"]))

    subtitle = structured.subtitle if structured and structured.subtitle else report.report_name
    if subtitle and subtitle.lower() != title_text.lower():
        story.append(_p(subtitle, styles["subtitle"]))

    meta_parts = [
        f"<b>Generated:</b> {report.generated_at.strftime('%d %b %Y, %H:%M UTC')}",
        f"<b>Records:</b> {report.records_analyzed:,}",
        f"<b>Scope:</b> Supplied report data",
    ]
    story.append(Paragraph(" &nbsp;&nbsp;·&nbsp;&nbsp; ".join(meta_parts), styles["meta"]))
    story.append(Spacer(1, 3 * mm))
    story.append(HRFlowable(width="100%", thickness=1.5, color=BRAND, spaceAfter=8))

    # 2. Key Metrics - 2x2 Grid of Styled KPI Cards (Same as Frontend)
    raw_metrics = []
    if structured and structured.metrics:
        raw_metrics = structured.metrics
    elif report.key_metrics:
        raw_metrics = report.key_metrics

    if raw_metrics:
        cards = []
        card_w = (width - 6 * mm) / 2
        for m in raw_metrics:
            lbl = getattr(m, "label", "")
            val = getattr(m, "value", "")
            note = getattr(m, "note", None)
            unit = getattr(m, "unit", "")
            if unit == "percent" and not str(val).endswith("%"):
                val = f"{val}%"
            val_display = _format_kpi_value(lbl, val)

            card_cells = [
                [Paragraph(escape(lbl).upper(), styles["kpi_label"])],
                [Paragraph(escape(val_display), styles["kpi_val"])],
            ]
            if note:
                card_cells.append([Paragraph(escape(str(note)), styles["kpi_note"])])

            card_table = Table(card_cells, colWidths=[card_w])
            card_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
                ("BOX", (0, 0), (-1, -1), 0.75, GRID),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ]))
            cards.append(card_table)

        # Build 2-column grid
        grid_rows = []
        for i in range(0, len(cards), 2):
            row = [cards[i]]
            if i + 1 < len(cards):
                row.extend(["", cards[i + 1]])
            else:
                row.extend(["", ""])
            if grid_rows:
                grid_rows.append(["", "", ""])
            grid_rows.append(row)

        row_heights = [4 * mm if r[0] == "" else None for r in grid_rows]
        grid_table = Table(grid_rows, colWidths=[card_w, 6 * mm, card_w], rowHeights=row_heights)
        grid_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(KeepTogether([
            Paragraph("Key Metrics", styles["h1"]),
            Spacer(1, 1.5 * mm),
            grid_table,
            Spacer(1, 4 * mm)
        ]))

    # 3. Executive Summary - Callout Box with Brand Left Accent (Same as Frontend)
    summary_heading = structured.executive_summary.heading if structured and structured.executive_summary else "Executive Summary"
    summary_paragraphs = structured.executive_summary.paragraphs if structured and structured.executive_summary else ([content.executive_summary] if content and content.executive_summary else [])

    if summary_paragraphs:
        summary_cells = [
            Paragraph(f"<b>{escape(summary_heading)}</b>", styles["summary_h"])
        ]
        for p_text in summary_paragraphs:
            if p_text.strip():
                summary_cells.append(Paragraph(escape(p_text.strip()), styles["body"]))
        if structured and structured.executive_summary.highlights:
            for hl in structured.executive_summary.highlights:
                if hl.strip():
                    summary_cells.append(Paragraph(f"• &nbsp;{escape(hl.strip())}", styles["bullet"]))

        summary_box = Table([[summary_cells]], colWidths=[width])
        summary_box.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
            ("LINELEFT", (0, 0), (0, -1), 3.5, BRAND),
            ("BOX", (0, 0), (-1, -1), 0.6, GRID),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ]))
        story.append(KeepTogether([
            Paragraph("Executive Summary", styles["h1"]),
            Spacer(1, 1.5 * mm),
            summary_box,
            Spacer(1, 4 * mm)
        ]))

    # 4. Visual Chart - Donut Chart with Center Total & Legend (Single Chart Area)
    rendered_tables = set()
    if structured and structured.charts:
        for chart in _resolve_export_charts(structured.charts, report.prompt):
            chart_bytes = render_png(chart)
            if chart_bytes is None:  # e.g. a geographic map: show the exact data instead of an approximation
                columns, rows = chart_table(chart)
                story.append(KeepTogether([
                    Paragraph(escape(chart.title), styles["h1"]),
                    Paragraph(escape(FALLBACK_NOTE), styles["body"]),
                    Spacer(1, 1.5 * mm),
                    _table(columns, rows, width, styles, bold),
                    Spacer(1, 4 * mm)
                ]))
                continue
            is_circular = (chart.type or "").lower() in {"pie", "doughnut", "donut", "gauge", "radar"}
            chart_img = Image(BytesIO(chart_bytes), width=width, height=width * (0.50 if is_circular else 0.44))
            chart_card = Table([[chart_img]] + ([[Paragraph(escape(chart.description), styles["body"])]] if chart.description else []),
                               colWidths=[width])
            chart_card.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("BOX", (0, 0), (-1, -1), 0.6, GRID),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]))
            story.append(KeepTogether([
                Paragraph(escape(chart.title), styles["h1"]),
                Spacer(1, 1.5 * mm),
                chart_card,
                Spacer(1, 4 * mm)
            ]))

    # 5. Datatables & Sections
    def append_table(title: str, cols: list[str], rws: list[list[str]]) -> None:
        t_flow = _table(cols, rws, width, styles, bold)
        title_p = Paragraph(escape(title), styles["h1"])
        if len(rws) <= 14:
            # Table fits on one page: keep title and table together so it NEVER leaves an orphan header
            story.append(KeepTogether([title_p, Spacer(1, 1.5 * mm), t_flow, Spacer(1, 3 * mm)]))
        else:
            story.append(title_p)
            story.append(Spacer(1, 1.5 * mm))
            story.append(t_flow)
            story.append(Spacer(1, 3 * mm))

    if structured and structured.sections:
        for section in structured.sections:
            for block in section.blocks:
                if block.type == "paragraph":
                    story.append(Paragraph(escape(section.title), styles["h1"]))
                    story.extend(_paragraphs(block.text, styles))
                elif block.type == "bullet_list":
                    story.append(ListFlowable(
                        [ListItem(_p(item, styles["body"])) for item in block.items],
                        bulletType="bullet", leftIndent=12
                    ))
                elif block.type == "table":
                    t_title = block.title or section.title
                    rendered_tables.add(t_title.strip().lower())
                    append_table(t_title, block.columns, block.rows)

    # Also render any tables from report.tables that were not inside sections
    if report.tables:
        for table in report.tables:
            if table.title.strip().lower() not in rendered_tables:
                append_table(table.title, table.columns, table.rows)
                rendered_tables.add(table.title.strip().lower())

    # 6. Key Findings / Attention Panel
    findings = (structured.findings if structured and structured.findings else (content.findings if content else []))
    if findings:
        story.append(Paragraph("Key Findings & Observations", styles["h1"]))
        for finding in findings:
            sev = (finding.severity or "info").lower()
            color = SEVERITY_COLORS.get(sev, BRAND)
            badge_text = f"<font size='7.5' color='{color.hexval()}'><b>[{sev.upper()}]</b></font>"
            header_p = Paragraph(f"<b>{escape(finding.title)}</b> &nbsp; {badge_text}", styles["body"])
            detail_p = Paragraph(escape(finding.detail), styles["indent"])

            finding_box = Table([[header_p], [detail_p]], colWidths=[width])
            finding_box.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
                ("LINELEFT", (0, 0), (0, -1), 3, color),
                ("BOX", (0, 0), (-1, -1), 0.5, GRID),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ]))
            story.append(KeepTogether([finding_box, Spacer(1, 2 * mm)]))
        story.append(Spacer(1, 2 * mm))

    # 7. Actionable Recommendations
    recommendations = (structured.recommendations if structured and structured.recommendations else (content.recommendations if content else []))
    if recommendations:
        story.append(Paragraph("Recommendations", styles["h1"]))
        rec_items = []
        for rec in recommendations:
            prio = (getattr(rec, "priority", "") or "medium").lower()
            prio_color = SEVERITY_COLORS.get(prio, BRAND)
            parts = [
                Paragraph(
                    f"<b>{escape(rec.action)}</b> &nbsp;<font size='7.5' color='{prio_color.hexval()}'><b>[{prio.upper()} PRIORITY]</b></font>",
                    styles["body"]
                )
            ]
            if getattr(rec, "rationale", None):
                parts.append(_p(rec.rationale, styles["indent"]))
            rec_items.append(ListItem(parts))
        story.append(ListFlowable(rec_items, bulletType="1", leftIndent=14, bulletFontName=bold, bulletFontSize=9.5))
        story.append(Spacer(1, 3 * mm))

    # 8. Scope & Data Limitations
    limitations = structured.limitations if structured and structured.limitations else (content.data_limitations if content else [])
    if limitations:
        story.append(Paragraph("Data Scope & Limitations", styles["h1"]))
        story.append(ListFlowable(
            [ListItem(_p(item, styles["body"]), leftIndent=12) for item in limitations],
            bulletType="bullet", start="•", leftIndent=12,
        ))
        story.append(Spacer(1, 3 * mm))

    # 9. Disclaimer
    if report.disclaimer:
        story.append(_p(report.disclaimer, styles["disclaimer"]))

    # Page Header & Footer Template
    def decorate(canvas, document) -> None:
        canvas.saveState()
        page_width, page_height = A4
        canvas.setStrokeColor(GRID)
        canvas.setLineWidth(0.5)

        # Header running line
        canvas.line(document.leftMargin, page_height - 12 * mm, page_width - document.rightMargin, page_height - 12 * mm)
        canvas.setFont(regular, 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(document.leftMargin, page_height - 10 * mm, report.report_name)
        canvas.drawRightString(page_width - document.rightMargin, page_height - 10 * mm, "Confidential")

        # Footer running line
        canvas.line(document.leftMargin, 12 * mm, page_width - document.rightMargin, 12 * mm)
        canvas.drawString(document.leftMargin, 8 * mm, f"Generated {report.generated_at:%d %b %Y, %H:%M UTC}")
        canvas.drawRightString(page_width - document.rightMargin, 8 * mm, f"Page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return buffer.getvalue()
