"""The analysis plan: what a report will compute, decided before any number is calculated.

A plan is plain data (filters, a date range, table specs, output preferences). It never contains
code or expressions: ``validate_plan`` checks every field, operator and value against the report
type's vocabulary, so an AI-suggested plan can be no more powerful than a rule-based one.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Any

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


@dataclass(frozen=True)
class AnalysisPlan:
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
