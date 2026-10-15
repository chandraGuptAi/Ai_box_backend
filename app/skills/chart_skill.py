"""Chart Skill - Server-executable charting and visual analytics engine.

Provides full programmatic and server-side capability to:
1. Detect user's requested chart types and topics dynamically without rigid boundations.
2. Build standardized ReportChart objects from shipment/financial metrics or raw records.
3. Render high-resolution charts as PNG bytes, Base64 data URLs, or SVG markup.
4. Provide system instructions and blueprints for AI models running on the server.
"""
from __future__ import annotations

import base64
from typing import Any, Literal
import xml.etree.ElementTree as ET

from app.charts import SKILLS, build_charts, get_adapter, plan_charts
from app.charts.render import render_png
from app.generators.chart_renderer import _COLORS
from app.models.report_schema import ReportChart, ChartDataset
from app.prompts.report_intent import detect_intent, report_charts, extract_chart_specs
from app.prompts.shipment_prompt import parse_order, compute_metrics


class ChartSkill:
    """Server-side charting skill for AI Box backend."""

    PALETTE: list[str] = list(_COLORS)
    # Every chart type registered in the chart skills engine ("donut" is kept as an alias).
    SUPPORTED_TYPES: list[str] = [*SKILLS, "donut"]

    def __init__(self) -> None:
        pass

    def get_supported_types(self) -> list[str]:
        """Return the list of all supported chart types."""
        return list(self.SUPPORTED_TYPES)

    def get_palette(self) -> list[str]:
        """Return the standard visual hex color palette."""
        return list(self.PALETTE)

    def get_skill_blueprint(self) -> dict[str, Any]:
        """Return the complete server-side skill blueprint and operational guidelines."""
        return {
            "name": "chart-crafting-skill",
            "version": "1.0.0",
            "description": (
                "Server-side chart crafting skill for multi-type visual analytics "
                "(Area, Line, Bar, Pie, Donut) with Highcharts-style variable radius, "
                "anti-collision callouts, and 1:1 PDF/screen fidelity."
            ),
            "supported_types": self.SUPPORTED_TYPES,
            "skills": [skill.describe() for skill in SKILLS.values()],
            "palette": self.PALETTE,
            "chart_type_specs": {
                "area": "Continuous or categorical data with semi-transparent fill and point markers.",
                "line": "Trend or category progression with circular markers and value annotations.",
                "bar": "Discrete metric comparisons with vertical or horizontal bars and value labels.",
                "donut": "Variable radius slices with center metric label and total value.",
                "pie": "Proportional distribution of parts to whole with pointer callouts."
            },
            "prompt_rules": [
                "Parse explicit chart sequences (e.g. 1. Area Chart, 2. Line Chart) dynamically.",
                "Never force a bar chart into a donut if the user requested a bar chart.",
                "Preserve user requested chart types across screen UI, REST responses, and PDF exports."
            ]
        }

    def detect_preferences(self, prompt: str) -> list[dict[str, Any]]:
        """Extract explicit chart specifications from user prompt."""
        return extract_chart_specs(prompt)

    def craft_charts_from_metrics(self, metrics: dict[str, Any], prompt: str = "") -> list[ReportChart]:
        """Craft optimal ReportChart objects from calculated metrics and user prompt."""
        intent = detect_intent(prompt)
        
        class MockAnalysis:
            pass
        analysis = MockAnalysis()
        analysis.metrics = metrics
        analysis.records_analyzed = metrics.get("scope", {}).get("records_analyzed", 100) or 100
        analysis.status_scope = None

        return report_charts(analysis, intent)

    def craft_charts_from_data(self, records: list[dict[str, Any]], prompt: str = "") -> list[ReportChart]:
        """Parse raw shipment records and craft charts based on user intent."""
        plan = plan_charts(prompt, "shipment", get_adapter("shipment"))
        if plan.requests:  # explicit chart types: the chart skills engine
            return build_charts("shipment", {"getOrderList": records}, plan).charts
        orders = [parse_order(r) for r in records]
        metrics = compute_metrics(orders)
        metrics["scope"] = {
            "source_records": len(records),
            "records_analyzed": len(orders),
            "single_status": False,
        }
        return self.craft_charts_from_metrics(metrics, prompt)

    def render_png(self, chart: ReportChart) -> bytes:
        """Render a ReportChart into crisp PNG bytes."""
        return render_png(chart) or b""

    def render_base64(self, chart: ReportChart) -> str:
        """Render a ReportChart into a base64 data URL string."""
        png_bytes = self.render_png(chart)
        encoded = base64.b64encode(png_bytes).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    def render_svg(self, chart: ReportChart, width: int = 600, height: int = 300) -> str:
        """Render a clean SVG representation of the chart."""
        chart_type = (chart.type or "bar").lower()
        root = ET.Element(
            "svg",
            xmlns="http://www.w3.org/2000/svg",
            viewBox=f"0 0 {width} {height}",
            width=str(width),
            height=str(height),
            style="background: #ffffff; font-family: sans-serif;"
        )

        # Title
        title_el = ET.SubElement(root, "text", x="20", y="28", fill="#1E293B", style="font-size: 14px; font-weight: 600;")
        title_el.text = str(chart.title)

        labels = [str(l) for l in chart.labels]
        dataset = chart.datasets[0] if chart.datasets else ChartDataset(label="Values", data=[])
        values = dataset.data
        colors = [self.PALETTE[i % len(self.PALETTE)] for i in range(len(labels))]

        if chart_type in {"line", "area"} and len(labels) >= 2:
            max_v = max(values) if values and max(values) > 0 else 1.0
            pL, pR, pT, pB = 40, 20, 50, 40
            plotW = width - pL - pR
            plotH = height - pT - pB
            
            pts = []
            for i, val in enumerate(values):
                x = pL + (plotW * i) / (len(labels) - 1)
                y = pT + plotH * (1.0 - (val / max_v))
                pts.append((x, y))

            pts_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)

            if chart_type == "area":
                area_pts = f"{pts[0][0]:.1f},{pT+plotH} {pts_str} {pts[-1][0]:.1f},{pT+plotH}"
                ET.SubElement(root, "polygon", points=area_pts, fill=colors[0], opacity="0.45")

            ET.SubElement(root, "polyline", points=pts_str, fill="none", stroke=colors[0], style="stroke-width: 2.5;")

            for (x, y), val, lbl in zip(pts, values, labels):
                ET.SubElement(root, "circle", cx=f"{x:.1f}", cy=f"{y:.1f}", r="4", fill=colors[0], stroke="#ffffff", style="stroke-width: 1.5;")
                lbl_el = ET.SubElement(root, "text", x=f"{x:.1f}", y=f"{height - 15}", fill="#64748B", style="font-size: 10px; text-anchor: middle;")
                lbl_el.text = lbl[:10]

        elif chart_type in {"pie", "doughnut", "donut"}:
            # Center circular chart reference tag
            cx, cy, r = width / 2, height / 2 + 10, min(width, height) / 3
            is_donut = chart_type in {"doughnut", "donut"}
            ET.SubElement(root, "circle", cx=str(cx), cy=str(cy), r=str(r), fill=colors[0], opacity="0.85")
            if is_donut:
                ET.SubElement(root, "circle", cx=str(cx), cy=str(cy), r=str(r * 0.45), fill="#ffffff")
                total_val = sum(values)
                txt = ET.SubElement(root, "text", x=str(cx), y=str(cy + 4), fill="#1E293B", style="font-size: 14px; font-weight: bold; text-anchor: middle;")
                txt.text = f"{total_val:,.0f}" if float(total_val).is_integer() else f"{total_val:,.1f}"

        else:
            # Bar chart default
            max_v = max(values) if values and max(values) > 0 else 1.0
            pL, pR, pT, pB = 40, 20, 50, 40
            plotW = width - pL - pR
            plotH = height - pT - pB
            n = max(len(labels), 1)
            barW = (plotW / n) * 0.7

            for i, (lbl, val, col) in enumerate(zip(labels, values, colors)):
                barH = plotH * (val / max_v)
                bx = pL + (plotW / n) * i + ((plotW / n) - barW) / 2
                by = pT + plotH - barH
                ET.SubElement(root, "rect", x=f"{bx:.1f}", y=f"{by:.1f}", width=f"{barW:.1f}", height=f"{barH:.1f}", fill=col, rx="4")
                lbl_el = ET.SubElement(root, "text", x=f"{bx + barW/2:.1f}", y=f"{height - 15}", fill="#64748B", style="font-size: 10px; text-anchor: middle;")
                lbl_el.text = lbl[:10]

        return ET.tostring(root, encoding="utf-8").decode("utf-8")


# Global singleton instance for easy import across the server
chart_skill = ChartSkill()
