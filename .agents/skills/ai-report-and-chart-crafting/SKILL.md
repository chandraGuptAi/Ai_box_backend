---
name: ai-report-and-chart-crafting
description: Best-practice blueprint and runbook for generating visual-first analytical shipment & financial reports using DeepSeek with Highcharts-style variable radius donut/pie charts, tabular data, and 1:1 PDF exports.
---

# AI Report & Visual Chart Crafting Skill

This skill defines the architectural blueprint and operational runbook for generating reports with **DeepSeek** and the **Bigship AI Box** backend.

---

## 1. Core Architecture: The Division of Labor

To achieve zero numerical hallucination and beautiful visual presentation, the system strictly separates responsibilities:

```
[User Request / Prompt]
          │
          ▼
┌─────────────────────────┐
│  Python Data Engine     │ ➔ Parses raw data, calculates aggregates, KPI cards,
│  (100% Deterministic)   │    rates, tables & field coverage.
└───────────┬─────────────┘
            │ Verified Metrics & Scoped Context
            ▼
┌─────────────────────────┐
│  DeepSeek AI Model      │ ➔ Writes executive narrative, prioritized findings,
│  (Structured JSON)      │    and actionable recommendations into strict schema.
└───────────┬─────────────┘
            │ Verified Narrative
            ▼
┌─────────────────────────┐
│  Chart & Export Engine  │ ➔ Renders Highcharts-style Variable Radius Donut/Pie charts
│  (Screen SVG & PDF)     │    and builds pixel-matched PDF / DOCX reports.
└─────────────────────────┘
```

- **DeepSeek's Job:** Synthesizes context, identifies operational bottlenecks, writes succinct executive summaries, and classifies issue severity (`high`, `medium`, `info`).
- **Python's Job:** Computes exact metrics, extracts datatables, enforces validation rules, and renders charts.
- **Frontend / PDF's Job:** Renders identical charts and cards on screen and in downloaded documents.

---

## 2. Visual Standards: Highcharts-Style Charts

All circular charts (Donut and Pie) generated in this project follow Highcharts **Variable Radius** specifications:

### Slices
- **Variable Outer Radius:** Slices with larger volume/share extend further outward ($r_{min}$ to $r_{max}$), creating an intuitive visual hierarchy.
- **Doughnut Hole:** Inner radius $r_{in} = 0.38$ for donut charts; $r_{in} = 0$ for solid pie charts.
- **White Borders:** 1.5px clean white boundaries separating wedges.

### Center Total (Donut Charts)
- Center displays the primary metric name (e.g. `Total Freight`, `Total Orders`) on line 1 in muted slate (`#64748B`).
- Line 2 displays the bold, formatted value (e.g. `₹60,221` or `76`) in dark slate (`#1E293B`).

### Pointer Callout Lines & Labels
- Slices with significant share ($\ge 3\%$) project elbow pointer lines outward.
- Lines match the slice color.
- Labels format: `<Category Name>  <Percentage%>` (e.g. `Delhivery 5KG  39.5%`).
- **Anti-Collision:** Left and right callouts are sorted vertically and spaced with minimum vertical gap to prevent overlapping text.

### Modern Color Palette
Consistent across Angular frontend (`chart.component.ts`) and Python backend (`chart_renderer.py`):
```python
COLORS = [
    "#5b5fe9",  # Royal Blue / Primary Brand
    "#16a34a",  # Emerald Green (Positive / Delivered)
    "#ef4444",  # Crimson Red (Exceptions / Cancelled)
    "#f59e0b",  # Amber / Warning
    "#9aa1b2",  # Slate Grey
    "#3b82f6",  # Sky Blue
    "#8b5cf6",  # Violet Purple
    "#14b8a6",  # Teal
]
```

---

## 3. DeepSeek Prompt Engineering Rules

When querying DeepSeek for report generation, adhere to the following prompt rules in `app/prompts/`:

1. **Strict JSON Output:** Output must conform to `StructuredNarrative` (title, executive summary, sections with table references, findings, recommendations).
2. **Never Invent Numbers:** All numbers must come from the supplied backend context. Any hallucinated metric triggers backend rejection.
3. **Intent Detection & Customization:**
   - **Omission Support:** If the user specifies `"hata do"`, `"remove"`, or `"without limitations"`, set `limitations: []` and omit the Data Scope section.
   - **Multi-Table Requests:** When the user asks for `"all tables"`, `"sare jaruri table"`, or `"multiple tables"`, activate topics `courier`, `payment`, `status`, `cancellation`, and `geography`.
   - **Dual Circular Charts:** When user mentions `"dono graph me donut pie"`, provide Donut as Chart 1 (Status / Undelivery) and Pie as Chart 2 (Courier / Payment).

---

## 4. Codebase Reference Map

File | Purpose
:--- | :---
`app/prompts/report_intent.py` | Intent detection, table selection, limitations handling, and chart configuration.
`app/prompts/shipment_prompt.py` | Data parsing, deterministic metric calculations, and table builders.
`app/prompts/base.py` | System prompt builders for DeepSeek with JSON schema constraints.
`app/services/deepseek_service.py` | Async client communicating with DeepSeek API.
`app/generators/chart_renderer.py` | In-memory Highcharts-style Variable Radius chart generation (PNG).
`app/generators/pdf_generator.py` | Native vector ReportLab PDF generation with matching KPI cards and charts.
`src/app/modules/ai_box/components/chart/` | Angular frontend SVG chart component.
`src/app/modules/ai_box/components/report-viewer/` | Unified screen viewer for AI reports.
