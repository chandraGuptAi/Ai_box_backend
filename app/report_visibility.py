"""Keep internal issue diagnostics out of customer reports and exports."""
from __future__ import annotations

import re
from typing import Any


_ISSUE = re.compile(
    r"\b(?:issues?|flagged|anomal(?:y|ies)|data quality|data gaps?|tracking inconsistenc\w*|"
    r"delivery date on file but|status still open|courier remark says delivered|"
    r"cancellation note on an active|pickup date earlier than|shipping charge over|"
    r"test orders|courier status feed needs fixing|check not run|field missing)\b", re.I)
_INTERNAL_KEYS = {
    "issues", "issue", "flagged", "flags", "order_checks", "data_gaps", "data_quality_notes",
    "delivered_but_open", "cancel_noted", "delivered_date_but_open", "courier_remark_delivered",
    "cancel_note_on_active", "pickup_before_order", "charge_outlier", "test_orders", "outlier", "verification",
}


def _issue_text(value: Any) -> bool:
    return isinstance(value, str) and bool(_ISSUE.search(value.replace("_", " ")))


_NEUTRAL = {"flagged": "marked", "issues": "concerns", "issue": "concern", "anomalies": "outliers", "anomaly": "outlier"}


def customer_safe_text(text: str, fallback: str = "Requested analysis") -> str:
    """Reword a model-chosen heading so the diagnostics filter below cannot hide content the user asked for."""
    if not _issue_text(text):
        return text
    def neutral(match):
        word = match.group(0)
        replacement = _NEUTRAL.get(word.lower(), "")
        return replacement.capitalize() if replacement and word[:1].isupper() else replacement
    text = " ".join(_ISSUE.sub(neutral, text.replace("_", " ")).split())
    return text if text and not _issue_text(text) else fallback


def customer_safe_id(identifier: str, fallback: str) -> str:
    """The same rewording for snake_case identifiers such as requirement IDs."""
    if not _issue_text(identifier):
        return identifier
    return re.sub(r"[^a-z0-9]+", "_", customer_safe_text(identifier, fallback).lower()).strip("_")[:47] or fallback


def customer_report(value: Any) -> Any:
    """Return a copy, preserving shipment rows and financial figures, excluding diagnostics."""
    if isinstance(value, list):
        return [customer_report(item) for item in value
                if not _issue_text(item) and not (
                    isinstance(item, dict) and any(_issue_text(item.get(key)) for key in
                        ("key", "label", "title", "heading", "content", "detail", "action", "rationale", "note")))]
    if not isinstance(value, dict):
        return value
    result = {key: item if key in {"limitations", "data_limitations"} else customer_report(item)
              for key, item in value.items() if key not in _INTERNAL_KEYS}
    # Keep display rows, typed values and column metadata aligned when hiding diagnostic columns.
    columns = value.get("columns")
    if isinstance(columns, list) and all(isinstance(c, str) for c in columns):
        keep = [i for i, column in enumerate(columns) if not _issue_text(column)
                and column.lower().replace(" ", "_") not in _INTERNAL_KEYS]
        if len(keep) != len(columns):
            result["columns"] = [columns[i] for i in keep]
            for key in ("rows", "values", "column_meta"):
                if isinstance(value.get(key), list):
                    result[key] = ([value[key][i] for i in keep] if key == "column_meta" else
                                   [[row[i] for i in keep] for row in value[key]])
    for key in ("takeaway", "note", "description", "executive_summary"):
        if _issue_text(result.get(key)):
            sentences = re.split(r"(?<=[.!?])\s+", result[key])
            result[key] = " ".join(s for s in sentences if not _issue_text(s)) or "Shipment report."
    return result
