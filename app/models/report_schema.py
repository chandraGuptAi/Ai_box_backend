"""Pydantic models for report requests, AI output and exportable report documents."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from app.config import settings

REPORT_TYPE_PATTERN = r"^[a-z][a-z0-9_]{1,49}$"

DEFAULT_DISCLAIMER = (
    "This report was generated with AI assistance from the data supplied at generation time. "
    "Figures in the metrics and tables were calculated directly from that data; narrative "
    "interpretation should be reviewed before it is used for business decisions."
)

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(value: Any) -> Any:
    """Strip characters that are invalid in DOCX/PDF output and surrounding whitespace."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        text = _CONTROL_CHARS.sub("", value).strip()
        if re.search(r"<\s*/?\s*[a-zA-Z][^>]*>", text):
            raise ValueError("HTML is not supported in report text")
        return text
    return value


def _normalize_report_type(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip().lower().replace("-", "_")
    return value


def _limit_list(max_items: int) -> BeforeValidator:
    """Accept a single item or null as a list and drop items past ``max_items``."""

    def _validator(value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, list):
            value = [value]
        return value[:max_items]

    return BeforeValidator(_validator)


def _level(allowed: tuple[str, ...], default: str, aliases: dict[str, str]) -> BeforeValidator:
    def _validator(value: Any) -> str:
        text = str(value or "").strip().lower()
        text = aliases.get(text, text)
        return text if text in allowed else default

    return BeforeValidator(_validator)


Text = Annotated[str, BeforeValidator(clean_text)]
ReportType = Annotated[str, BeforeValidator(_normalize_report_type), Field(pattern=REPORT_TYPE_PATTERN)]

Severity = Annotated[
    Literal["high", "medium", "low", "info"],
    _level(
        ("high", "medium", "low", "info"),
        "info",
        {"critical": "high", "major": "high", "severe": "high", "moderate": "medium",
         "minor": "low", "informational": "info", "information": "info", "positive": "info"},
    ),
]
Priority = Annotated[
    Literal["high", "medium", "low"],
    _level(
        ("high", "medium", "low"),
        "medium",
        {"critical": "high", "urgent": "high", "moderate": "medium", "minor": "low"},
    ),
]


# --- Request ----------------------------------------------------------------

class ReportRequest(BaseModel):
    """Payload sent by Angular to generate a report."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "report_type": "shipment",
                    "prompt": "Analyze shipment status and cancellation reasons",
                    "data": {
                        "getOrderList": [
                            {"order_id": "BS1001", "order_status": "Delivered", "courier_name": "Delhivery",
                             "payment_type": "Prepaid", "state": "Maharashtra", "order_date": "2025-01-03"},
                            {"order_id": "BS1002", "order_status": "Cancelled", "courier_name": "Xpressbees",
                             "payment_type": "COD", "cancellation_reason": "Customer not available",
                             "state": "Delhi", "order_date": "2025-01-04"},
                        ]
                    },
                }
            ]
        }
    )

    report_type: ReportType = Field(..., description="Report type key, e.g. 'shipment'. See GET /api/reports/types.")
    prompt: Text = Field(
        default="",
        max_length=settings.max_prompt_chars,
        description="Optional user instructions that steer the focus of the analysis.",
    )
    data: dict[str, Any] = Field(
        ...,
        description="Source data. For shipment reports, orders are read from data.getOrderList.",
    )
    previous_report: dict[str, Any] | None = Field(
        default=None,
        description="Optional existing report to refine or append new sections to incrementally.",
    )


# --- AI generated content ---------------------------------------------------

class ReportSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    heading: Text = Field(..., min_length=1, max_length=200)
    content: Text = Field(..., min_length=1, max_length=8000)


class ReportFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Text = Field(..., min_length=1, max_length=250)
    detail: Text = Field(..., min_length=1, max_length=3000)
    severity: Severity = "info"


class ReportRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Text = Field(..., min_length=1, max_length=600)
    rationale: Text = Field(default="", max_length=3000)
    priority: Priority = "medium"


class ReportContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    """Narrative content produced by the AI model (validated before use)."""

    title: Text = Field(..., min_length=1, max_length=200)
    executive_summary: Text = Field(..., min_length=1, max_length=6000)
    sections: Annotated[list[ReportSection], _limit_list(12)] = Field(default_factory=list)
    findings: Annotated[list[ReportFinding], _limit_list(20)] = Field(default_factory=list)
    recommendations: Annotated[list[ReportRecommendation], _limit_list(15)] = Field(default_factory=list)
    data_limitations: Annotated[list[Annotated[Text, Field(min_length=1, max_length=1000)]], _limit_list(15)] = Field(
        default_factory=list
    )


# --- Python calculated content ----------------------------------------------

class KeyMetric(BaseModel):
    label: Text = Field(..., min_length=1, max_length=120)
    value: Text = Field(..., min_length=1, max_length=200)
    note: Text | None = Field(default=None, max_length=300)


class ReportTable(BaseModel):
    title: Text = Field(..., min_length=1, max_length=200)
    columns: list[Annotated[Text, Field(max_length=100)]] = Field(..., min_length=1, max_length=12)
    rows: list[list[Annotated[Text, Field(max_length=500)]]] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _rows_match_columns(self) -> ReportTable:
        for index, row in enumerate(self.rows):
            if len(row) != len(self.columns):
                raise ValueError(
                    f"Row {index} has {len(row)} cells but the table has {len(self.columns)} columns."
                )
        return self


# --- Report document / response --------------------------------------------

class ReportDocument(BaseModel):
    """A complete report. Accepted by the export endpoints.

    The Angular client can post back the object returned by /generate as-is
    (extra fields such as ``metrics`` are ignored by the exporters).
    """

    report_type: ReportType
    report_name: Text = Field(..., min_length=1, max_length=120)
    generated_at: datetime
    prompt: Text = Field(default="", max_length=settings.max_prompt_chars)
    records_analyzed: int = Field(default=0, ge=0)
    key_metrics: list[KeyMetric] = Field(default_factory=list, max_length=30)
    tables: list[ReportTable] = Field(default_factory=list, max_length=15)
    content: ReportContent
    structured_report: StructuredReport | None = None
    # Report schema 3.0: deterministic shipment insights (KPIs, issues, couriers, account mix, charts).
    # Calculated in Python only; optional so v2 clients and saved reports keep working.
    insights: dict[str, Any] | None = None
    disclaimer: Text = Field(default=DEFAULT_DISCLAIMER, max_length=1500)


class ReportResponse(ReportDocument):
    """Response of POST /api/reports/generate."""

    report_id: str
    ai_model: str
    metrics: dict[str, Any] = Field(
        default_factory=dict,
        description="Full set of metrics calculated in Python (useful for charts in Angular).",
    )
    generation: dict[str, Any] | None = Field(
        default=None,
        description="How the narrative was produced: ai_calls (0 or 1), narrative_source "
                    "(ai, ai_repaired, deterministic_fallback, deterministic) and an optional user-facing notice.",
    )


class ReportTypeInfo(BaseModel):
    report_type: str
    display_name: str
    description: str
    default_prompt: str
    accepted_data_paths: list[str]


class SaveReportRequest(BaseModel):
    report_id: str
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    report: dict[str, Any] | None = None


class RenameReportRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)


# --- Errors (for OpenAPI documentation) ------------------------------------

class ErrorBody(BaseModel):
    code: str
    category: Literal["validation", "report_generation", "export", "server"]
    message: str
    details: Any = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ParagraphBlock(StrictModel):
    type: Literal["paragraph"]
    text: Text = Field(min_length=1, max_length=1200)


class BulletBlock(StrictModel):
    type: Literal["bullet_list"]
    items: list[Annotated[Text, Field(min_length=1, max_length=500)]] = Field(min_length=1, max_length=12)


class TableReference(StrictModel):
    type: Literal["table_reference"]
    table_id: str = Field(pattern=r"^table_[0-9]+$")


NarrativeBlock = Annotated[ParagraphBlock | BulletBlock | TableReference, Field(discriminator="type")]


class StructuredSummary(StrictModel):
    heading: Text = "Executive Summary"
    paragraphs: list[Annotated[Text, Field(min_length=1, max_length=1200)]] = Field(min_length=1, max_length=4)
    highlights: list[Annotated[Text, Field(min_length=1, max_length=500)]] = Field(default_factory=list, max_length=5)


class StructuredSection(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,59}$")
    title: Text = Field(min_length=1, max_length=200)
    order: int = Field(ge=1, le=50)
    blocks: list[NarrativeBlock] = Field(min_length=1, max_length=12)


class StructuredNarrative(StrictModel):
    title: Text = Field(min_length=1, max_length=200)
    executive_summary: StructuredSummary
    sections: list[StructuredSection] = Field(default_factory=list, max_length=50)
    findings: list[ReportFinding] = Field(default_factory=list, max_length=30)
    recommendations: list[ReportRecommendation] = Field(default_factory=list, max_length=20)
    data_limitations: list[Annotated[Text, Field(min_length=1, max_length=1000)]] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def unique_sections(self):
        if len({s.id for s in self.sections}) != len(self.sections) or len({s.order for s in self.sections}) != len(self.sections):
            raise ValueError("Section ids and orders must be unique")
        self.sections.sort(key=lambda s: s.order)
        return self


class TableBlock(ReportTable):
    type: Literal["table"] = "table"


class DisplaySection(StrictModel):
    id: str
    title: Text
    order: int
    blocks: list[Annotated[ParagraphBlock | BulletBlock | TableBlock, Field(discriminator="type")]]
    source_prompt: Text | None = None


class DisplayMetric(StrictModel):
    key: str
    label: Text
    value: float | int | str
    unit: Literal["number", "percent", "text"]
    priority: Literal["primary", "secondary"] = "primary"
    note: Text | None = None
    tone: Literal["normal", "ok", "bad", "warning"] | None = None
    icon: Text | None = None


ChartType = Literal["bar", "horizontal_bar", "grouped_bar", "stacked_bar", "line", "area", "pie", "doughnut", "donut",
                    "scatter", "bubble", "histogram", "heatmap", "boxplot", "treemap", "funnel", "radar", "waterfall",
                    "gauge", "map"]
_Finite = Annotated[float, Field(allow_inf_nan=False)]


class ChartPoint(StrictModel):
    """One scatter/bubble point. `r` is the bubble size (record count)."""
    x: _Finite
    y: _Finite
    r: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    label: Text | None = None


class BoxStats(StrictModel):
    min: _Finite
    q1: _Finite
    median: _Finite
    q3: _Finite
    max: _Finite
    count: int = Field(ge=1)


class ChartAxis(StrictModel):
    field: str | None = None
    label: Text
    unit: Text | None = None
    format: Literal["category", "date", "number", "currency", "percent"] = "category"


class ChartValueFormat(StrictModel):
    """Numbers stay numeric; the client formats them with this metadata."""
    type: Literal["number", "currency", "percent"] = "number"
    decimals: int = Field(default=0, ge=0, le=4)
    currency: Text | None = None     # None: as supplied in the source data
    unit: Text | None = None         # e.g. "kg"


class ChartDataset(StrictModel):
    label: Text
    data: list[Annotated[float, Field(ge=0, allow_inf_nan=False)]] = Field(max_length=200)
    points: list[ChartPoint] | None = Field(default=None, max_length=500)
    boxes: list[BoxStats] | None = Field(default=None, max_length=200)


class ReportChart(StrictModel):
    id: str
    title: Text
    type: ChartType = "bar"
    labels: list[Text] = Field(min_length=1, max_length=200)
    datasets: list[ChartDataset] = Field(min_length=1, max_length=10)
    # Optional chart-engine metadata (absent on legacy charts; see CHARTS.md).
    x_axis: ChartAxis | None = None
    y_axis: ChartAxis | None = None
    value_format: ChartValueFormat | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    description: Text | None = Field(default=None, max_length=300)
    source: Literal["rule", "ai_plan", "default"] | None = None

    @model_validator(mode="after")
    def matching_lengths(self):
        if self.type in {"scatter", "bubble"}:
            if any(not d.points or d.data for d in self.datasets):
                raise ValueError("Scatter and bubble datasets carry points, not data")
            if self.type == "bubble" and any(len(d.points) != len(self.labels) for d in self.datasets):
                raise ValueError("Bubble labels and points must have equal lengths")
            return self
        if any(len(d.data) != len(self.labels) for d in self.datasets):
            raise ValueError("Chart labels and data must have equal lengths")
        if any(d.boxes is not None and len(d.boxes) != len(self.labels) for d in self.datasets):
            raise ValueError("Box plot labels and boxes must have equal lengths")
        return self


class ChartNote(StrictModel):
    """A requested chart that could not be produced, and why (the rest of the report still succeeds)."""
    requested_type: Text
    title: Text | None = None
    reason: Text
    missing: list[Text] = Field(default_factory=list)


class StructuredReport(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    report_id: str
    report_type: ReportType
    title: Text
    subtitle: Text
    generated_at: datetime
    executive_summary: StructuredSummary
    metrics: list[DisplayMetric]
    sections: list[DisplaySection]
    findings: list[ReportFinding]
    recommendations: list[ReportRecommendation]
    limitations: list[Text]
    charts: list[ReportChart] = Field(default_factory=list)
    chart_notes: list[ChartNote] = Field(default_factory=list)
    scope: dict[str, Any]

ReportDocument.model_rebuild()
ReportResponse.model_rebuild()
