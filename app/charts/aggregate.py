"""Predefined, deterministic aggregations. Charts only ever use these functions."""
from __future__ import annotations

import math
from collections import OrderedDict
from datetime import date, timedelta
from typing import Any, Iterable

from app.charts.fields import DatasetAdapter, MeasureSpec


def valid_number(value: Any) -> float | None:
    """A finite, non-negative number, else None (null, text, NaN, infinity and negatives are invalid)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def matches(row: dict[str, Any], where: Iterable[tuple[str, frozenset]]) -> bool:
    return all(row.get(field) in values for field, values in where)


def apply_filters(rows: list[dict[str, Any]], filters: Iterable[tuple[str, Any]]) -> list[dict[str, Any]]:
    filters = tuple(filters)
    return [r for r in rows if all(r.get(f) == v for f, v in filters)]


def window(rows: list[dict[str, Any]], date_field: str, days: int) -> tuple[list[dict[str, Any]], date | None]:
    """Rows from the last `days` days, anchored on the latest date in the data (not today)."""
    dates = [r[date_field] for r in rows if isinstance(r.get(date_field), date)]
    if not dates:
        return [], None
    end = max(dates)
    start = end - timedelta(days=days - 1)
    return [r for r in rows if isinstance(r.get(date_field), date) and start <= r[date_field] <= end], end


def measure(rows: list[dict[str, Any]], spec: MeasureSpec) -> float | None:
    """Value of one predefined measure over a group of rows; None when the group has no valid input."""
    if spec.agg == "share":
        return None  # Requires the declared section denominator; resolved by the report executor.
    if spec.agg == "count":
        return float(sum(1 for r in rows if matches(r, spec.where)))
    if spec.agg == "rate":
        return round(100.0 * sum(1 for r in rows if matches(r, spec.where)) / len(rows), 1) if rows else None
    values = [v for v in (valid_number(r.get(spec.field)) for r in rows) if v is not None]
    if not values:
        return None
    from decimal import Decimal, ROUND_HALF_UP
    import statistics
    numbers = [Decimal(str(value)) for value in values]
    total = sum(numbers)
    value = (total if spec.agg == "sum" else statistics.median(numbers) if spec.agg == "median"
             else max(numbers) if spec.agg == "max" else total / len(numbers))
    return float(value.quantize(Decimal(".01"), rounding=ROUND_HALF_UP))


def group(rows: list[dict[str, Any]], field: str, adapter: DatasetAdapter) -> "OrderedDict[str, list[dict[str, Any]]]":
    groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in rows:
        groups.setdefault(adapter.label_of(field, row.get(field)), []).append(row)
    return groups


def date_buckets(rows: list[dict[str, Any]], field: str, granularity: str | None, max_points: int):
    """Continuous day/week/month buckets (empty periods included), coarsened to fit `max_points`.

    Returns (granularity, [(label, rows)]), or (None, []) when there are no valid dates.
    """
    dated = [r for r in rows if isinstance(r.get(field), date)]
    if not dated:
        return None, []
    first, last = min(r[field] for r in dated), max(r[field] for r in dated)
    order = ["day", "week", "month"]
    gran = granularity if granularity in order else "day"
    for gran in order[order.index(gran):]:
        keys = _periods(first, last, gran)
        if len(keys) <= max_points:
            break
    buckets: OrderedDict[date, list[dict[str, Any]]] = OrderedDict((k, []) for k in keys)
    for row in dated:
        buckets[_period_start(row[field], gran)].append(row)
    if len(buckets) > max_points:  # even monthly is too long: keep the latest periods
        buckets = OrderedDict(list(buckets.items())[-max_points:])
    return gran, [(_period_label(k, gran), v) for k, v in buckets.items()]


def _period_start(day: date, gran: str) -> date:
    if gran == "week":
        return day - timedelta(days=day.weekday())
    if gran == "month":
        return day.replace(day=1)
    return day


def _periods(first: date, last: date, gran: str) -> list[date]:
    keys, current = [], _period_start(first, gran)
    while current <= last:
        keys.append(current)
        if gran == "day":
            current += timedelta(days=1)
        elif gran == "week":
            current += timedelta(days=7)
        else:
            current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    return keys


def _period_label(start: date, gran: str) -> str:
    if gran == "month":
        return start.strftime("%b %Y")
    if gran == "week":
        return "Week of " + start.isoformat()
    return start.isoformat()


def quartiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)

    def at(p: float) -> float:
        k = (len(ordered) - 1) * p
        lo, hi = math.floor(k), math.ceil(k)
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)

    return {"min": round(ordered[0], 2), "q1": round(at(.25), 2), "median": round(at(.5), 2),
            "q3": round(at(.75), 2), "max": round(ordered[-1], 2), "count": len(ordered)}


def histogram(values: list[float], max_bins: int = 12) -> list[tuple[str, int]]:
    """Equal-width bins (Sturges' rule, capped). Every value is counted exactly once."""
    lo, hi = min(values), max(values)
    if lo == hi:
        return [(_num(lo), len(values))]
    bins = max(2, min(max_bins, math.ceil(math.log2(len(values))) + 1))
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        counts[min(int((v - lo) / width), bins - 1)] += 1
    return [(f"{_num(lo + i * width)}–{_num(lo + (i + 1) * width)}", c) for i, c in enumerate(counts)]


def _num(value: float) -> str:
    return f"{value:,.0f}" if abs(value) >= 100 else f"{value:,.2f}".rstrip("0").rstrip(".")
