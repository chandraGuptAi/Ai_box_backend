"""Build the requested charts from request data. Every number is computed here, in Python."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.charts.aggregate import apply_filters, window
from app.charts.fields import get_adapter
from app.charts.planner import MAX_CHARTS, ChartPlan, merge_ai_requests
from app.charts.skills import SKILLS, BuildContext, ChartRequest, ChartUnavailable
from app.models.report_schema import ChartNote, ReportChart

logger = logging.getLogger(__name__)


@dataclass
class ChartResult:
    charts: list[ReportChart] = field(default_factory=list)
    notes: list[ChartNote] = field(default_factory=list)


def build_charts(report_type: str, data: dict[str, Any], plan: ChartPlan, *, ai_requests: Any = None,
                 status_scope: str | None = None) -> ChartResult:
    """Charts for every request in `plan`; an unusable request becomes a ChartNote, never an error."""
    result = ChartResult()
    adapter = get_adapter(report_type)
    if adapter is None:
        result.notes = [ChartNote(requested_type=SKILLS[r.type].label, reason="Charts are not available for this report type.")
                        for r in plan.requests]
        return result
    requests = merge_ai_requests(plan, ai_requests, adapter) if plan.unresolved else list(plan.requests)
    try:
        rows = adapter.rows(data)
    except Exception:  # the report's own analysis already validated the data; never fail the report here
        logger.exception("Chart rows could not be extracted.")
        rows = []
    if status_scope and adapter.status_field:
        rows = [r for r in rows if r.get(adapter.status_field) == status_scope]
    used_ids: set[str] = set()
    for request in requests[:MAX_CHARTS]:
        skill = SKILLS[request.type]
        try:
            chart = skill.build(request, _context(request, adapter, rows))
        except ChartUnavailable as exc:
            result.notes.append(ChartNote(requested_type=skill.label, title=request.title, reason=exc.reason,
                                          missing=[adapter.fields[m].label if m in adapter.fields else m for m in exc.missing]))
            continue
        chart.id = _unique_id(request, used_ids, adapter)
        # The chart's own subset, so its values can be recomputed independently (see app/analysis/verify.py).
        if request.filters:
            chart.options = {**chart.options, "applied_filters": [list(f) for f in request.filters]}
        if request.window_days:
            chart.options = {**chart.options, "window_days": request.window_days}
        result.charts.append(chart)
    for request in requests[MAX_CHARTS:]:
        result.notes.append(ChartNote(requested_type=SKILLS[request.type].label,
                                      reason=f"At most {MAX_CHARTS} charts are generated per report."))
    return result


def _context(request: ChartRequest, adapter, rows) -> BuildContext:
    rows = apply_filters(rows, request.filters)
    note = None
    if request.window_days:
        if not adapter.date_field:
            raise ChartUnavailable("This report type has no date field, so a time window cannot be applied.")
        rows, end = window(rows, adapter.date_field, request.window_days)
        if end is None:
            label = adapter.fields[adapter.date_field].label.lower()
            raise ChartUnavailable(f"No valid {label}s in the supplied records, so the last {request.window_days} days "
                                   "cannot be shown.", (adapter.date_field,))
        note = f"Last {request.window_days} days of the supplied data, up to {end.isoformat()}."
    return BuildContext(adapter=adapter, rows=rows, note=note)


# Ids the previous chart code used for the same chart, so existing clients keep matching them.
_LEGACY_IDS = {("courier", "orders"): "courier_orders", ("status_category", "orders"): "shipment_status",
               ("courier", "shipping_charges"): "courier_charges", ("payment_mode", "shipping_charges"): "payment_charges",
               ("payment_mode", "orders"): "payment_orders", ("destination_state", "orders"): "destination_orders"}


def _unique_id(request: ChartRequest, used: set[str], adapter=None) -> str:
    legacy = _LEGACY_IDS.get((request.dimension or (adapter.default_dimension.get(request.type) if adapter else None),
                              request.measure or (adapter.default_measure if adapter else None)))
    if legacy and request.type in {"bar", "line", "area", "pie", "doughnut"} and not request.series and not request.filters:
        legacy += "_pie" if legacy == "courier_charges" and request.type in {"pie", "doughnut"} else ""
    else:
        legacy = None
    base = legacy or re.sub(r"[^a-z0-9_]+", "_", f"{request.type}_{request.dimension or request.measure or 'chart'}".lower()).strip("_")
    candidate, n = base, 2
    while candidate in used:
        candidate, n = f"{base}_{n}", n + 1
    used.add(candidate)
    return candidate
