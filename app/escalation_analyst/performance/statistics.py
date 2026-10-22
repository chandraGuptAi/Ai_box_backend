"""Nearest-rank percentile statistics with the original JavaScript rounding."""

from __future__ import annotations
from ..utils.js_compat import js_round


def stats(values: list) -> dict:
    values = sorted(v for v in values if v is not None)
    if not values:
        return dict(count=0, avg=None, median=None, p90=None, max=None, buckets=None)
    at = lambda p: values[min(len(values) - 1, js_round(p * (len(values) - 1)))]
    ranges = [
        ("Under 1 hour", 0, 3600),
        ("1\u20134 hours", 3600, 14400),
        ("4\u201324 hours", 14400, 86400),
        ("Over 24 hours", 86400, float("inf")),
    ]
    return dict(
        count=len(values),
        avg=sum(values) / len(values),
        median=at(0.5),
        p90=at(0.9),
        max=values[-1],
        buckets=[
            dict(label=label, count=sum(low <= v < high for v in values)) for label, low, high in ranges
        ],
    )
