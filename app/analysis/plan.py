"""The analysis plan: what a report will compute, decided before any number is calculated.

A plan is plain data (filters, a date range, table specs, output preferences). It never contains
code or expressions: ``validate_plan`` checks every field, operator and value against the report
type's vocabulary, so an AI-suggested plan can be no more powerful than a rule-based one.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Any
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.analysis.vocabulary import PlanVocabulary
from app.charts.fields import DatasetAdapter

OPS = ("eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "between")
_OP_WORDS = {"eq": "is", "ne": "is not", "in": "is", "not_in": "is not", "gt": "above", "gte": "at least",
             "lt": "below", "lte": "at most", "between": "between"}
DETAIL_LEVELS = ("summary", "standard", "detailed")


@dataclass(frozen=True)
class Filter:
    field: str
    op: str
    value: Any                       # str | tuple[str, ...] | float | tuple[float, float]
    text: str = ""                   # prompt words that produced it

    def describe(self, adapter: DatasetAdapter, vocab: PlanVocabulary) -> str:
        label = vocab.label(self.field, adapter)
        numeric = vocab.is_numeric(self.field, adapter)

        def show(value):
            if numeric:
                return f"{value:,.2f}" if vocab.column_type(self.field, adapter) == "currency" else f"{value:,g}"
            return adapter.label_of(self.field, value)

        if self.op == "between":
            return f"{label} between {show(self.value[0])} and {show(self.value[1])}"
        if self.op in ("in", "not_in"):
            values = " or ".join(show(v) for v in self.value)
            return f"{label} {_OP_WORDS[self.op]} {values}"
        return f"{label} {_OP_WORDS[self.op]} {show(self.value)}"


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date
    label: str                       # the user's words, e.g. "last month"
    basis: str                       # how it was resolved, e.g. "calendar month before 13 Oct 2026 (as_of)"
    field: str = "order_date"

    def describe(self) -> str:
        return f"{_day(self.start)} to {_day(self.end)}"


@dataclass(frozen=True)
class TableSpec:
    kind: str                        # "detail" (one row per record) | "grouped" (one row per group)
    title: str
    columns: tuple[str, ...] = ()    # detail: field names; grouped: measure names
    group_by: str | None = None
    series: str | None = None        # grouped: second category, e.g. payment mode within each city (a cross-tab)
    comparison: bool = False         # the table answers a requested comparison (it replaces overview sections)
    granularity: str | None = None   # grouped by date: day | week | month
    sort: str | None = None
    descending: bool = True
    limit: int | None = None


@dataclass(frozen=True)
class Unsupported:
    requested: str
    reason: str
    missing: tuple[str, ...] = ()


class ExecutionFilter(BaseModel):
    """A registered field/operator pair; validated before data is fetched."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    field: str
    op: Literal["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "between"]
    value: str | float | list[str] | list[float]

    @model_validator(mode="after")
    def registered_filter(self):
        from app.charts.fields import get_adapter
        adapter = get_adapter("shipment")
        if self.field not in adapter.fields:
            raise ValueError("Unregistered filter field")
        numeric = adapter.fields[self.field].kind == "number"
        if numeric:
            values = self.value if isinstance(self.value, list) else [self.value]
            if self.op not in {"gt", "gte", "lt", "lte", "between"} or any(isinstance(v, bool) or not isinstance(v, (int, float)) or _number(v) is None for v in values):
                raise ValueError("Invalid numeric filter")
            if (self.op == "between" and (len(values) != 2 or values[0] > values[1])) or (self.op != "between" and isinstance(self.value, list)):
                raise ValueError("Invalid numeric bounds")
        elif adapter.fields[self.field].kind == "date":
            values = self.value if isinstance(self.value, list) else [self.value]
            if self.op not in {"eq", "ne", "gt", "gte", "lt", "lte", "between"}:
                raise ValueError("Invalid date operator")
            if (self.op == "between" and len(values) != 2) or (self.op != "between" and isinstance(self.value, list)):
                raise ValueError("Invalid date bounds")
            if not all(isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) for v in values):
                raise ValueError("Dates must use ISO YYYY-MM-DD")
            days = [date.fromisoformat(v) for v in values]
            if self.op == "between" and days[0] > days[1]:
                raise ValueError("Date bounds must be ordered")
        elif adapter.fields[self.field].kind != "category" or self.op not in {"eq", "ne", "in", "not_in"}:
            raise ValueError("Only registered category/numeric analysis filters are supported")
        elif self.op in {"in", "not_in"}:
            if not isinstance(self.value, list) or not self.value or not all(isinstance(v, str) and v.strip() for v in self.value):
                raise ValueError("Membership filters need nonempty strings")
        elif not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("Category filters need a string")
        if self.field == "status_category":
            from app.analysis.vocabulary import get_vocabulary
            values = self.value if isinstance(self.value, list) else [self.value]
            if any(v not in get_vocabulary("shipment").status_values for v in values):
                raise ValueError("Unknown normalized status")
        if self.field == "rto_stage":
            values = self.value if isinstance(self.value, list) else [self.value]
            if any(v not in {"pending", "completed", "unknown", "not_rto"} for v in values):
                raise ValueError("Unknown return stage")
        if self.field == "shipment_status":
            from app.prompts.shipment_prompt import normalize_shipment_status
            values = self.value if isinstance(self.value, list) else [self.value]
            normalized = [normalize_shipment_status(v) for v in values]
            object.__setattr__(self, "value", normalized if isinstance(self.value, list) else normalized[0])
        return self


class ExecutionSection(BaseModel):
    """Content is selected by the model; operations are selected from the Python registry."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    title: str = Field(min_length=1, max_length=160)
    requirement_ids: tuple[str, ...] = Field(min_length=1, max_length=8)
    dimensions: tuple[str, ...] = Field(default=(), max_length=3)
    metrics: tuple[str, ...] = Field(min_length=1, max_length=8)
    filters: tuple[ExecutionFilter, ...] = Field(default=(), max_length=8)
    required_fields: tuple[str, ...] = Field(default=(), max_length=20)
    ranking_metric: str | None = None
    direction: Literal["desc", "asc"] = "desc"
    limit: int = Field(default=20, ge=1, le=200)
    charts: tuple[Literal["bar", "pie", "doughnut", "line", "area", "histogram", "scatter"], ...] = ()
    missing_data: Literal["explicit_unavailable", "clarify"] = "explicit_unavailable"
    narrative: Literal["summary", "recommendations", "summary_and_recommendations"] = "summary"

    @model_validator(mode="after")
    def registered_components(self):
        from app.charts.fields import get_adapter
        adapter = get_adapter("shipment")
        if any(d not in adapter.fields or adapter.fields[d].kind not in {"category", "date"} for d in self.dimensions):
            raise ValueError("Grouping dimension is not registered")
        if any(m not in adapter.measures for m in self.metrics):
            raise ValueError("Metric operation is not registered")
        if self.ranking_metric and self.ranking_metric not in self.metrics:
            raise ValueError("Ranking metric must appear in the section")
        if any(f not in adapter.fields for f in self.required_fields):
            raise ValueError("Required field is not registered")
        if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", r) for r in self.requirement_ids):
            raise ValueError("Invalid requirement ID")
        if len(set(self.requirement_ids)) != len(self.requirement_ids) or len(set(self.dimensions)) != len(self.dimensions) or len(set(self.metrics)) != len(self.metrics):
            raise ValueError("Duplicate requirement, dimension or metric")
        return self


class ReportExecutionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_kind: Literal["general_analysis", "specific_analysis", "unrelated", "needs_clarification"] = "specific_analysis"
    clarification_kind: Literal["optional_preferences", "scope_conflict", "missing_capability", "essential"] = "essential"
    report_title: str | None = Field(default=None, min_length=1, max_length=72)
    intent: str = Field(min_length=1, max_length=160)
    required_source: Literal["shipments"] = "shipments"
    source_scope: dict
    cohort: tuple[ExecutionFilter, ...] = Field(default=(), max_length=8)
    sections: tuple[ExecutionSection, ...] = Field(default=(), max_length=20)
    requirement_ids: tuple[str, ...] = Field(default=(), max_length=40)
    clarifications: tuple[str, ...] = Field(default=(), max_length=5)
    limitations: tuple[str, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def coverage(self):
        ids = [s.id for s in self.sections]
        requirements = [r for s in self.sections for r in s.requirement_ids]
        if len(set(ids)) != len(ids) or len(set(self.requirement_ids)) != len(self.requirement_ids):
            raise ValueError("Duplicate section or requirement IDs")
        if set(requirements) != set(self.requirement_ids):
            raise ValueError("Every requirement must have a section")
        if not self.sections and not self.clarifications and self.request_kind not in {"general_analysis", "unrelated"}:
            raise ValueError("An accepted plan needs requested sections")
        if not self.sections and not self.clarifications and self.request_kind == "general_analysis" and self.cohort:
            raise ValueError("A scoped analysis needs planned sections; it cannot use a general overview default")
        return self


@dataclass(frozen=True)
class AnalysisPlan:
    planner_calls: int = 0
    report_title: str | None = None
    detail_level: str = "standard"
    lookup: bool = False             # "show/list these records": answered by Python alone
    narrative: bool = True           # False: no DeepSeek call is needed
    wants_charts: bool = False
    wants_tables: bool = False
    recommendations: bool = True
    filters: tuple[Filter, ...] = ()
    date_range: DateRange | None = None
    tables: tuple[TableSpec, ...] = ()
    topics: tuple[str, ...] = ()
    unsupported: tuple[Unsupported, ...] = ()
    notes: tuple[str, ...] = ()      # how ambiguous wording was interpreted
    answerable: bool = True
    ambiguous: bool = False
    source: str = "rules"            # "rules" | "rules+ai"
    sections: tuple[ExecutionSection, ...] = ()
    intent: str = "shipment_analysis"

    @property
    def scoped(self) -> bool:
        return bool(self.filters or self.date_range)

    def single_status(self, status_field: str | None) -> str | None:
        for f in self.filters:
            if f.field == status_field:
                if f.op == "eq":
                    return f.value
                if f.op == "in" and len(f.value) == 1:
                    return f.value[0]
        return None

    def describe_scope(self, adapter: DatasetAdapter, vocab: PlanVocabulary) -> list[str]:
        parts = [f.describe(adapter, vocab) for f in self.filters]
        if self.date_range:
            parts.append(f"{vocab.label(self.date_range.field, adapter)} from {self.date_range.describe()}")
        return parts

    def to_dict(self, adapter: DatasetAdapter, vocab: PlanVocabulary, chart_types: list[str] | None = None) -> dict[str, Any]:
        """JSON-safe plan, returned with the report so the client can show what was applied."""
        return {
            "source": self.source,
            "detail_level": self.detail_level,
            "lookup": self.lookup,
            "ai_narrative": self.narrative,
            "recommendations": self.recommendations,
            "filters": [{"field": f.field, "op": f.op, "value": list(f.value) if isinstance(f.value, tuple) else f.value,
                         "description": f.describe(adapter, vocab)} for f in self.filters],
            "date_range": None if not self.date_range else {
                "field": self.date_range.field, "from": self.date_range.start.isoformat(),
                "to": self.date_range.end.isoformat(), "requested": self.date_range.label, "basis": self.date_range.basis},
            "tables": [{"kind": t.kind, "title": t.title, "group_by": t.group_by, "series": t.series, "columns": list(t.columns)} for t in self.tables],
            "charts": chart_types or [],
            "topics": list(self.topics),
            "unsupported": [{"requested": u.requested, "reason": u.reason, "missing": list(u.missing)} for u in self.unsupported],
            "notes": list(self.notes),
            "sections": [s.model_dump() for s in self.sections],
            "requirement_ids": list(dict.fromkeys(r for s in self.sections for r in s.requirement_ids)),
            "metric_definitions": {name: {"label": adapter.measures[name].label,
                "operation": adapter.measures[name].agg, "field": adapter.measures[name].field,
                "numerator_filters": [{"field": field, "values": sorted(values)} for field, values in adapter.measures[name].where],
                "denominator": "section shipments within each group" if adapter.measures[name].agg == "rate"
                    else "all section shipments" if adapter.measures[name].agg == "share"
                    else "valid metric values within group" if adapter.measures[name].agg == "avg" else None}
                for name in dict.fromkeys(m for s in self.sections for m in s.metrics)},
            "intent": self.intent,
            "report_title": self.report_title,
        }


def _day(value: date) -> str:
    return f"{value.day} {value.strftime('%b %Y')}"


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value and abs(value) != float("inf") and value >= 0 else None
    return None


def validate_plan(plan: AnalysisPlan, adapter: DatasetAdapter, vocab: PlanVocabulary,
                  rows: list[dict[str, Any]]) -> AnalysisPlan:
    """Drop anything outside the vocabulary; record requests the supplied data cannot answer."""
    unsupported = list(plan.unsupported)
    blocked = False                  # a requested subset that cannot be identified is never replaced by all records
    filters = []
    for f in plan.filters:
        if f.op not in OPS or not vocab.filterable(f.field, adapter):
            continue
        if vocab.is_numeric(f.field, adapter):
            values = f.value if f.op == "between" else (f.value,)
            if f.op in ("in", "not_in", "eq", "ne") or not isinstance(values, tuple) \
                    or any(_number(v) is None for v in values) or (f.op == "between" and len(values) != 2):
                continue
            if f.op == "between" and values[0] > values[1]:
                values = (values[1], values[0])
            value = tuple(float(v) for v in values) if f.op == "between" else float(values[0])
        else:
            if f.op not in ("eq", "ne", "in", "not_in"):
                continue
            items = f.value if isinstance(f.value, (tuple, list)) else (f.value,)
            items = tuple(dict.fromkeys(str(v) for v in items if isinstance(v, str) and v.strip()))
            if f.field == adapter.status_field:
                items = tuple(v for v in items if v in vocab.status_values)
            if not items:
                continue
            value = items if f.op in ("in", "not_in") else items[0]
        if not any(r.get(f.field) is not None for r in rows):
            label = vocab.label(f.field, adapter)
            unsupported.append(Unsupported(f.describe(adapter, vocab) if f.value is not None else label,
                                           f"No {label.lower()} values are present in the supplied records, so this filter "
                                           "cannot be applied.", (label,)))
            blocked = True
            continue
        filters.append(replace(f, value=value))
    date_range = plan.date_range
    if date_range and not any(r.get(date_range.field) is not None for r in rows):
        blocked = True
        label = vocab.label(date_range.field, adapter)
        unsupported.append(Unsupported(f"period {date_range.label}", f"No valid {label.lower()} values are present in the "
                                       "supplied records, so the requested period cannot be applied.", (label,)))
    tables = []
    for spec in plan.tables:
        if spec.kind == "detail":
            columns = tuple(c for c in spec.columns if c in vocab.detail_columns)
            if columns:
                tables.append(replace(spec, columns=columns[:vocab.max_detail_columns],
                                      limit=min(spec.limit or vocab.max_table_rows, vocab.max_table_rows)))
        elif spec.kind == "duration" and spec.group_by in adapter.fields:
            tables.append(spec)
        elif spec.kind == "grouped" and spec.group_by in adapter.fields and adapter.fields[spec.group_by].kind != "number":
            measures = tuple(m for m in spec.columns if m in adapter.measures)
            series = spec.series if (spec.series in adapter.fields and adapter.fields[spec.series].kind == "category"
                                     and spec.series != spec.group_by) else None
            if measures:
                tables.append(replace(spec, columns=measures[:6], series=series,
                                      limit=min(spec.limit or vocab.max_table_rows, vocab.max_table_rows)))
    detail = plan.detail_level if plan.detail_level in DETAIL_LEVELS else "standard"
    # The interpreter decides whether anything answerable was asked; here, only an inapplicable subset blocks it.
    answerable = plan.answerable and not blocked
    return replace(plan, filters=tuple(filters), date_range=None if blocked else date_range, tables=tuple(tables),
                   unsupported=tuple(dict.fromkeys(unsupported)), detail_level=detail, answerable=answerable)
