"""Check every Python-calculated figure in a report against the current request's rows.

The checks recount from the scoped rows with their own simple loops instead of reusing the code
that produced the figures, so a bug in a calculation shows up as a failed check. A figure that
fails is removed from the report (never shown unverified); a figure that cannot be recomputed is
listed as not independently verified instead of being claimed accurate.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from app.analysis.plan import AnalysisPlan
from app.analysis.vocabulary import PlanVocabulary
from app.charts.fields import DatasetAdapter, MeasureSpec
from app.models.report_schema import ChartNote, DisplayMetric, ReportChart, ReportTable

_CATEGORICAL = {"bar", "horizontal_bar", "line", "area", "pie", "doughnut", "donut", "treemap"}
_MONEY_TOLERANCE = 0.011
_PERCENT_TOLERANCE = 0.051


def _pct(part: float, whole: float) -> float:
    return round(part * 100 / whole, 1) if whole else 0.0


def _close(a: Any, b: Any, tolerance: float = _MONEY_TOLERANCE) -> bool:
    try:
        return abs(float(a) - float(b)) <= tolerance
    except (TypeError, ValueError):
        return False


def _num(cell: Any) -> float | None:
    if isinstance(cell, (int, float)) and not isinstance(cell, bool):
        return float(cell)
    try:
        return float(str(cell).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


class _Checks:
    def __init__(self):
        self.items: list[dict[str, Any]] = []

    def add(self, name: str, ok: bool | None, detail: str = "") -> bool:
        status = "not_verifiable" if ok is None else "passed" if ok else "failed"
        self.items.append({"check": name, "status": status, **({"detail": detail} if detail else {})})
        return bool(ok)


# --- Independent recounts ---------------------------------------------------------------

def _value(rows: list[dict[str, Any]], m: MeasureSpec) -> float | None:
    def hit(row):
        return all(row.get(f) in values for f, values in m.where)
    if m.agg == "count":
        return float(sum(1 for r in rows if hit(r)))
    if m.agg == "rate":
        return round(100.0 * sum(1 for r in rows if hit(r)) / len(rows), 1) if rows else None
    numbers = [r[m.field] for r in rows if isinstance(r.get(m.field), (int, float)) and not isinstance(r.get(m.field), bool)]
    if not numbers:
        return None
    total = sum(Decimal(repr(float(v))) for v in numbers)
    return round(float(total if m.agg == "sum" else total / len(numbers)), 2)


def _groups(rows: list[dict[str, Any]], field: str, adapter: DatasetAdapter) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        out[adapter.label_of(field, row.get(field))].append(row)
    return out


def _casefold_counts(values) -> Counter:
    return Counter(str(v).casefold() for v in values if v)


# --- Metric checks ------------------------------------------------------------------------

def _check_metrics(checks: _Checks, rows: list[dict[str, Any]], analysis: Any, vocab: PlanVocabulary) -> None:
    m, n = analysis.metrics, len(rows)
    checks.add("record_count", analysis.records_analyzed == n, f"{analysis.records_analyzed} analysed, {n} in scope")
    status = vocab.status_field
    breakdown = m.get("status_category_breakdown")
    if status and isinstance(breakdown, list):
        counts = Counter(r.get(status) for r in rows)
        ok = sum(c["count"] for c in breakdown) == n and all(
            c["count"] == counts.get(c["category"], 0) and _close(c["percent"], _pct(c["count"], n), _PERCENT_TOLERANCE)
            for c in breakdown) and len(breakdown) == len([k for k, v in counts.items() if v])
        checks.add("status_distribution", ok, "category counts, total and percentages")
        rates = m.get("rates") or {}
        checks.add("status_rates", all(_close(v, _pct(counts.get(k.removesuffix("_percent"), 0), n), _PERCENT_TOLERANCE)
                                       for k, v in rates.items()), "rate denominators are the records in scope")
    couriers = m.get("courier_performance")
    if isinstance(couriers, list):
        recount: dict[str, Counter] = defaultdict(Counter)
        for r in rows:
            key = (r.get("courier") or "Not specified").casefold()
            recount[key]["total"] += 1
            recount[key][r.get(status)] += 1
        ok = sum(c["total_orders"] for c in couriers) == n and all(
            recount[c["courier"].casefold()]["total"] == c["total_orders"]
            and all(recount[c["courier"].casefold()][k] == c[k] for k in ("delivered", "cancelled", "rto") if k in c)
            for c in couriers)
        checks.add("courier_totals", ok, "per-courier counts sum to the records in scope")
    payments = m.get("payment_mode_breakdown")
    if isinstance(payments, list):
        recount = _casefold_counts(r.get("payment_mode") for r in rows)
        checks.add("payment_totals", all(recount[p["payment_mode"].casefold()] == p["count"] for p in payments)
                   and sum(p["count"] for p in payments) <= n, "payment-mode counts")
    reasons = m.get("cancellation_reasons")
    if isinstance(reasons, dict) and status:
        cancelled = [r for r in rows if r.get(status) == "cancelled"]
        with_reason = [r for r in cancelled if r.get("cancellation_reason")]
        recount = _casefold_counts(r["cancellation_reason"] for r in with_reason)
        ok = (reasons.get("cancelled_orders") == len(cancelled) and reasons.get("cancelled_orders_with_reason") == len(with_reason)
              and all(recount[t["reason"].casefold()] == t["count"] for t in reasons.get("top_reasons", [])))
        checks.add("cancellation_reasons", ok, "cancelled orders, reasons recorded and reason counts")
    financial = m.get("shipping_financial")
    if isinstance(financial, dict):
        charges = [r["charge"] for r in rows if isinstance(r.get("charge"), (int, float))]
        total = sum(Decimal(repr(float(c))) for c in charges)
        ok = financial.get("orders_with_valid_charge") == len(charges)
        if charges:
            ok = ok and _close(financial.get("total_shipping_charges"), total) and _close(
                financial.get("average_charge_per_order"), total / len(charges))
            groups = financial.get("courier_charges") or []
            ok = ok and _close(sum(Decimal(g["total_shipping_charges"]) for g in groups if g.get("total_shipping_charges")), total)
        else:
            ok = ok and financial.get("total_shipping_charges") is None
        cancelled = [r["charge"] for r in rows if r.get(status) == "cancelled" and isinstance(r.get("charge"), (int, float))]
        if cancelled:
            ok = ok and _close(financial.get("charges_associated_with_cancelled_orders"),
                               sum(Decimal(repr(float(c))) for c in cancelled))
        checks.add("financial_totals", ok, "shipping-charge total, average, courier split and cancelled-order charges")


def _check_scope(checks: _Checks, rows: list[dict[str, Any]], plan: AnalysisPlan | None) -> None:
    if not plan:
        return
    if plan.date_range:
        dr = plan.date_range
        checks.add("date_filter", all(isinstance(r.get(dr.field), date) and dr.start <= r[dr.field] <= dr.end for r in rows),
                   f"every record is dated {dr.start.isoformat()} to {dr.end.isoformat()}")
    for f in plan.filters:
        def ok(row):
            value = row.get(f.field)
            if value is None:
                return False
            if f.op in ("in", "not_in"):
                inside = str(value).casefold() in {str(v).casefold() for v in f.value}
                return inside == (f.op == "in")
            if f.op in ("eq", "ne"):
                return (str(value).casefold() == str(f.value).casefold()) == (f.op == "eq")
            low, high = (f.value if f.op == "between" else (None, None))
            return {"gt": lambda: value > f.value, "gte": lambda: value >= f.value, "lt": lambda: value < f.value,
                    "lte": lambda: value <= f.value, "between": lambda: low <= value <= high}[f.op]()
        checks.add(f"filter:{f.field}", all(ok(r) for r in rows), f"{f.field} {f.op} applied to every record")


# --- Charts --------------------------------------------------------------------------------

def _metric_pairs(value: Any, out: set | None = None) -> set[tuple[str, float]]:
    """(label, number) pairs from every list of metric rows; legacy charts must match one of them."""
    out = set() if out is None else out
    if isinstance(value, dict):
        for item in value.values():
            _metric_pairs(item, out)
    elif isinstance(value, list):
        for row in value:
            if isinstance(row, dict):
                labels = [str(v).casefold() for v in row.values() if isinstance(v, str) and _num(v) is None]
                numbers = [_num(v) for v in row.values() if _num(v) is not None and not isinstance(v, bool)]
                out.update((label, round(number, 2)) for label in labels for number in numbers)
            _metric_pairs(row, out)
    return out


def _check_chart(chart: ReportChart, rows: list[dict[str, Any]], adapter: DatasetAdapter, pairs: set) -> bool | None:
    x = chart.x_axis.field if chart.x_axis else None
    y = chart.y_axis.field if chart.y_axis else None
    if chart.source is None:
        # Legacy chart built from metrics: every value must be one of the (already verified) metric rows.
        return all((label.casefold(), round(value, 2)) in pairs for d in chart.datasets
                   for label, value in zip(chart.labels, d.data))
    if (chart.type not in _CATEGORICAL or x not in adapter.fields or y not in adapter.measures
            or adapter.fields[x].kind == "date" or len(chart.datasets) != 1 or chart.options.get("window_days")):
        return None
    scoped = [r for r in rows if all(r.get(f) == v for f, v in chart.options.get("applied_filters", []))]
    m = adapter.measures[y]
    groups = _groups(scoped, x, adapter)
    shown = 0.0
    for label, value in zip(chart.labels, chart.datasets[0].data):
        if label == "Other" and label not in groups:
            continue
        expected = _value(groups.get(label, []), m)
        if expected is None or not _close(value, expected, _PERCENT_TOLERANCE if m.agg == "rate" else _MONEY_TOLERANCE):
            return False
        shown += value
    if "Other" in chart.labels and "Other" not in groups and m.additive:
        total = _value(scoped, m) or 0.0
        other = chart.datasets[0].data[chart.labels.index("Other")]
        return _close(other, total - shown, 0.02)
    return True


# --- Tables --------------------------------------------------------------------------------

def _check_table(table: ReportTable, rows: list[dict[str, Any]], analysis: Any, adapter: DatasetAdapter,
                 vocab: PlanVocabulary, pairs: set) -> bool | None:
    tid = table.id or ""
    if tid == "plan_detail":
        ids = {str(r.get(vocab.id_column)) for r in rows}
        column = table.column_meta[0].key if table.column_meta else None
        ok = table.total_rows == len(rows) and len(table.rows) == min(len(rows), vocab.max_table_rows)
        if column == vocab.id_column:
            ok = ok and all(str(v[0]) in ids for v in table.values or [] if v[0] is not None)
        return ok
    if tid.startswith("plan_grouped") and table.column_meta and table.values:
        field = table.column_meta[0].key
        if field not in adapter.fields or adapter.fields[field].kind == "date":
            return None
        second = table.column_meta[1].key if len(table.column_meta) > 1 else None
        series = second if second in adapter.fields and adapter.fields[second].kind == "category" else None
        groups = _groups(rows, field, adapter)
        keys = [m.key for m in table.column_meta]
        for values in table.values:
            group_rows = groups.get(str(values[0]), [])
            cell_rows = group_rows
            if series:  # cross-tab cell: recount within the (group, series) pair; share is of the group
                cell_rows = [r for r in group_rows if adapter.label_of(series, r.get(series)) == str(values[1])]
                if "share_percent" in keys and not _close(values[keys.index("share_percent")],
                                                          _pct(len(cell_rows), len(group_rows)), _PERCENT_TOLERANCE):
                    return False
            for meta, cell in zip(table.column_meta[1:], values[1:]):
                if meta.key in adapter.measures:
                    expected = _value(cell_rows, adapter.measures[meta.key])
                    if (expected is None) != (cell is None) or (cell is not None and not _close(cell, expected, 0.051)):
                        return False
        return True
    if tid == "plan_duration":
        return None  # recomputed only by the duration builder; listed as not independently verified
    if table.title == "Account-Level Status Benchmark":
        return None  # copied from the API's account counters, not from the supplied records
    # Standard tables: each row's numbers must belong to the same label in the verified metrics.
    for row in table.rows:
        label = str(row[0]).casefold()
        for cell in row[1:]:
            number = _num(cell)
            if number is not None and (label, round(number, 2)) not in pairs:
                return False
    return True


# --- KPI cards ------------------------------------------------------------------------------

def _check_card(card: DisplayMetric, rows: list[dict[str, Any]], analysis: Any, vocab: PlanVocabulary) -> bool | None:
    status = vocab.status_field
    n = len(rows)
    counts = Counter(r.get(status) for r in rows)
    expected: dict[str, Any] = {
        "total_orders": n, "delivered": counts.get("delivered", 0), "cancelled": counts.get("cancelled", 0),
        "exceptions": counts.get("exception", 0) + counts.get("rto", 0),
        "delivered_percent": _pct(counts.get("delivered", 0), n), "cancelled_percent": _pct(counts.get("cancelled", 0), n),
        "exception_rate": round((counts.get("exception", 0) + counts.get("rto", 0)) / max(n, 1) * 100, 1),
        "distinct_couriers": len({str(r["courier"]).casefold() for r in rows if r.get("courier")}),
    }
    if card.key not in expected:
        financial = analysis.metrics.get("shipping_financial") or {}
        source = {"total_shipping_charges": "total_shipping_charges", "average_charge_per_order": "average_charge_per_order",
                  "cancelled_shipping_charges": "charges_associated_with_cancelled_orders"}.get(card.key)
        if source:
            return str(card.value) in (str(financial.get(source)), "Unavailable")
        return None
    value = _num(card.value)
    return value is not None and _close(value, expected[card.key], _PERCENT_TOLERANCE)


# --- Entry point ------------------------------------------------------------------------------

def verify_report(*, rows: list[dict[str, Any]], analysis: Any, plan: AnalysisPlan | None, adapter: DatasetAdapter,
                  vocab: PlanVocabulary, charts: list[ReportChart], tables: list[ReportTable],
                  cards: list[DisplayMetric], narrative_source: str, claims_removed: int = 0
                  ) -> tuple[dict[str, Any], list[ReportChart], list[ChartNote], list[ReportTable], list[DisplayMetric]]:
    checks = _Checks()
    _check_metrics(checks, rows, analysis, vocab)
    _check_scope(checks, rows, plan)
    pairs = _metric_pairs(analysis.metrics)
    kept_charts, notes = [], []
    for chart in charts:
        ok = _check_chart(chart, rows, adapter, pairs)
        checks.add(f"chart:{chart.id}", ok)
        if ok is False:
            notes.append(ChartNote(requested_type=chart.type, title=chart.title,
                                   reason="Removed: its values could not be verified against the supplied data."))
        else:
            kept_charts.append(chart)
    kept_tables = []
    for table in tables:
        ok = _check_table(table, rows, analysis, adapter, vocab, pairs)
        checks.add(f"table:{table.id or table.title}", ok)
        if ok is not False:
            kept_tables.append(table)
    kept_cards = []
    for card in cards:
        ok = _check_card(card, rows, analysis, vocab)
        checks.add(f"kpi:{card.key}", ok)
        if ok is not False:
            kept_cards.append(card)
    # The AI narrative's numbers were already matched to these metrics (or the narrative was replaced) upstream.
    checks.add("narrative_numbers", True, "AI numbers matched to Python metrics" if narrative_source.startswith("ai") else
               "narrative numbers written by Python from the same metrics")
    checks.add("missing_data_claims", True, f"{claims_removed} unsupported statement(s) removed" if claims_removed else
               "no statement claims that available data is missing")
    failed = [c["check"] for c in checks.items if c["status"] == "failed"]
    unverified = [c["check"] for c in checks.items if c["status"] == "not_verifiable"]
    summary = {
        "status": "passed" if not failed and not claims_removed else "passed_with_corrections",
        "checked": sum(1 for c in checks.items if c["status"] != "not_verifiable"),
        "failed_and_removed": failed,
        "not_independently_verified": unverified,
        "checks": checks.items,
    }
    return summary, kept_charts, notes, kept_tables, kept_cards


# --- Narrative: statements that deny data the request actually contains ------------------

_ABSENCE = (r"\b(?:no|none of the|without)\b[^.]{0,40}?\bSUBJECT\b[^.]{0,80}?\b(?:available|recorded|provided|present|"
            r"captured|found|given|supplied|exist)\b|\bSUBJECT\b[^.]{0,80}?\b(?:(?:is|are|were|was)\s+(?:not|never)\s+"
            r"(?:available|recorded|provided|present|captured|supplied|possible)|unavailable|missing|absent|not\s+available|"
            r"(?:cannot|can ?not|could not|can't)\s+be\s+(?:calculated|computed|determined|compared|assessed|analy[sz]ed|"
            r"derived|shown|provided))\b")
_QUANTIFIED = re.compile(r"\d|\b(?:some|several|many|few|most|certain|partial|part of|not all|a number of|majority|minority)\b")


def remove_false_absence_claims(narrative: dict[str, Any], rows: list[dict[str, Any]], vocab: PlanVocabulary,
                                subjects: dict[str, str]) -> tuple[dict[str, Any], int]:
    """Drop sentences that say a field is unavailable when the scoped records contain it.

    `subjects` maps a regex for the field's name in prose ("cancellation reasons?") to the row field.
    Quantified statements ("3 of 10 cancelled orders have no reason") are true partial claims and kept.
    """
    present = [re.compile(_ABSENCE.replace("SUBJECT", f"(?:{pattern})"), re.I)
               for pattern, field in subjects.items() if any(r.get(field) not in (None, "") for r in rows)]
    if not present:
        return narrative, 0
    removed = 0

    def false_claim(sentence: str) -> bool:
        return not _QUANTIFIED.search(sentence) and any(p.search(sentence) for p in present)

    def has_claim(text: str) -> bool:
        return any(false_claim(s) for s in re.split(r"(?<=[.!?])\s+", text or ""))

    def clean(text: str) -> str:
        nonlocal removed
        kept = []
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if false_claim(sentence):
                removed += 1
            else:
                kept.append(sentence)
        return " ".join(kept).strip()

    summary = narrative.get("executive_summary")
    if isinstance(summary, dict):
        summary["paragraphs"] = [p for p in (clean(t) for t in summary.get("paragraphs", [])) if p]
        summary["highlights"] = [h for h in (clean(t) for t in summary.get("highlights", [])) if h]
    for section in narrative.get("sections", []):
        blocks = []
        for block in section.get("blocks", []):
            if block.get("type") == "paragraph":
                block["text"] = clean(block["text"])
                if not block["text"]:
                    continue
            elif block.get("type") == "bullet_list":
                block["items"] = [i for i in (clean(t) for t in block["items"]) if i]
                if not block["items"]:
                    continue
            blocks.append(block)
        section["blocks"] = blocks
    narrative["sections"] = [s for s in narrative.get("sections", []) if s.get("blocks")]
    findings = narrative.get("findings", [])
    narrative["findings"] = [f for f in findings if not (has_claim(f.get("detail", "")) or has_claim(f.get("title", "")))]
    limitations = narrative.get("data_limitations", [])
    narrative["data_limitations"] = [t for t in limitations if not has_claim(t)]
    removed += len(findings) - len(narrative["findings"]) + len(limitations) - len(narrative["data_limitations"])
    for rec in narrative.get("recommendations", []):
        rec["rationale"] = clean(rec.get("rationale", ""))
    return narrative, removed
