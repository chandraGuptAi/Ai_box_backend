"""Match the values a plan names to the values the fetched shipments actually contain.

The planner sees only the schema, so it names values the way a user would ("Pending", "delhivery",
"pickup_scheduled"). Before any row is filtered, each category value is resolved against this
request's own data: the same words in any spelling, a broad status that covers detailed ones, or a
partial courier/city name that identifies existing values. A value with no meaning in the data is kept
unchanged, so a genuinely empty result stays empty and is reported as such.
"""
from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Any

from app.analysis.plan import AnalysisPlan, ExecutionFilter, Filter, match_key

logger = logging.getLogger("ai_box.report_accuracy")

# Free-text categories where a partial name identifies values ("Delhivery" -> "Delhivery Surface").
_PARTIAL_FIELDS = {"courier", "destination_city", "destination_state", "zone", "cancellation_reason", "rto_reason"}
_MEMBERSHIP = {"eq": "in", "ne": "not_in", "in": "in", "not_in": "not_in"}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text))


def _resolve_value(field: str, value: Any, present: dict[str, str]) -> list[str]:
    """The data spellings this value means; [] when it means nothing in this data."""
    key = match_key(value)
    if key in present:
        return [present[key]]
    if field == "shipment_status":
        # A broad status ("Pending") means every detailed status of that kind ("Pickup Scheduled", "Not Picked").
        from app.prompts.shipment_prompt import categorize_status
        category = categorize_status(key)
        if category not in {"other", "unknown"}:
            return [spelling for k, spelling in present.items() if categorize_status(k) == category]
    if field in _PARTIAL_FIELDS and len(key) >= 3:
        wanted = _words(key)
        return [spelling for k, spelling in present.items() if wanted and wanted <= _words(k)]
    return []


def _resolve(field: str, op: str, value: Any, rows: list[dict[str, Any]]):
    """(op, value) after resolution, or None when nothing changes."""
    if op not in _MEMBERSHIP:
        return None
    values = list(value) if isinstance(value, (list, tuple)) else [value]
    if not all(isinstance(v, str) for v in values):
        return None
    present = {}
    for row in rows:
        actual = row.get(field)
        if isinstance(actual, str) and actual.strip():
            present.setdefault(match_key(actual), actual)
    if not present:
        return None
    resolved = []
    for item in values:
        resolved.extend(_resolve_value(field, item, present) or [item])
    resolved = list(dict.fromkeys(resolved))
    if [match_key(v) for v in resolved] == [match_key(v) for v in values]:
        return None
    return _MEMBERSHIP[op], resolved


_REFERENCE_FIELDS = ("order_reference", "awb_reference")


def _reference_field(condition: Filter, rows: list[dict[str, Any]]) -> Filter:
    """An "order" number that is really an AWB (or the reverse) is looked up where it exists."""
    wanted = {match_key(v) for v in condition.value}
    if any(match_key(r.get(condition.field)) in wanted for r in rows if r.get(condition.field)):
        return condition
    other = next(f for f in _REFERENCE_FIELDS if f != condition.field)
    if any(match_key(r.get(other)) in wanted for r in rows if r.get(other)):
        return Filter(other, condition.op, condition.value, condition.text)
    return condition


def resolve_plan_values(plan: AnalysisPlan, rows: list[dict[str, Any]]) -> AnalysisPlan:
    """The plan with every category filter value expressed in this data's own spelling."""
    changed = []
    filters = []
    for condition in plan.filters:
        if condition.field in _REFERENCE_FIELDS:
            resolved = _reference_field(condition, rows)
            if resolved is not condition:
                changed.append(resolved.field)
            filters.append(resolved)
            continue
        result = _resolve(condition.field, condition.op, condition.value, rows)
        if result:
            changed.append(condition.field)
            condition = Filter(condition.field, result[0], tuple(result[1]), condition.text)
        filters.append(condition)
    sections = []
    for section in plan.sections:
        section_filters = []
        for condition in section.filters:
            result = _resolve(condition.field, condition.op, condition.value, rows)
            if result:
                changed.append(condition.field)
                condition = ExecutionFilter(field=condition.field, op=result[0], value=result[1])
            section_filters.append(condition)
        sections.append(section if tuple(section_filters) == section.filters else
                        section.model_copy(update={"filters": tuple(section_filters)}))
    if not changed:
        return plan
    logger.info("[PASS] filter_values resolved_against_data fields=%s", ",".join(sorted(set(changed))))
    return replace(plan, filters=tuple(filters), sections=tuple(sections))
