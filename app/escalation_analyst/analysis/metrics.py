"""Machine-count extraction and deterministic, closed-weighted master totals."""

from __future__ import annotations

import json
import math
import re
from ..utils.js_compat import js_round, fixed

METRIC_KEYS = [
    "raised",
    "closed",
    "verified_closed",
    "claimed_closed_unconfirmed",
    "promised_not_done",
    "pending",
    "responded_meaningful",
    "formality_only",
    "missed",
    "high_panic",
    "critical",
    "abuse_legal",
    "follow_ups_seller",
    "staff_responses_to_followups",
    "first_mile",
    "last_mile",
    "avg_hours_to_close",
    "avg_days_to_close",
    "best_case_count",
    "worst_case_count",
]
FLOAT_KEYS = {"avg_hours_to_close", "avg_days_to_close"}


def parse_metrics(text: str) -> dict | None:
    fences = re.findall(r"```json\s*([\s\S]*?)```", text or "", re.I)
    if not fences:
        fences = [
            value
            for value in re.findall(r"```\s*([\s\S]*?)```", text or "")
            if re.search(r'"raised"\s*:', value)
        ]
    if not fences:
        fences = re.findall(r'\{[^{}]*"raised"[\s\S]*?\}', text or "")
    if not fences:
        return None
    try:
        values = json.loads(fences[-1].strip())
        if not isinstance(values, dict):
            return None
    except ValueError:
        return None
    out = {}
    for key in METRIC_KEYS:
        value = values.get(key)
        try:
            number = float(0 if value is None or value == "" else value)
        except (ValueError, TypeError):
            number = math.nan
        out[key] = (
            (number if math.isfinite(number) else None)
            if key in FLOAT_KEYS
            else js_round(number) if math.isfinite(number) else 0
        )
        if key in FLOAT_KEYS and value is None:
            out[key] = None
    return out


def sum_metrics(metrics: list[dict]) -> dict:
    out = {key: sum((m.get(key) or 0) for m in metrics) for key in METRIC_KEYS if key not in FLOAT_KEYS}
    for key in FLOAT_KEYS:
        available = [m for m in metrics if m.get(key) is not None]
        weight = lambda m: m["closed"] if (m.get("closed") or 0) > 0 else 1
        denominator = sum(weight(m) for m in available)
        out[key] = fixed(sum(m[key] * weight(m) for m in available) / denominator) if denominator else None
    return out
