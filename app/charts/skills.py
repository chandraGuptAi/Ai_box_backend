"""Chart skills: one registered builder per chart type.

A skill declares its aliases, required inputs, supported data shape and point limit, and builds a
``ReportChart`` from Python-aggregated rows. When the data cannot support it, it raises
``ChartUnavailable`` with a specific reason; the report still succeeds. Add a chart type by
calling ``register()``; the report service never changes.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from app.charts.aggregate import date_buckets, group, histogram, matches, measure, quartiles, valid_number
from app.charts.fields import DatasetAdapter, FieldSpec, MeasureSpec
from app.charts.geo import canonical_state
from app.models.report_schema import (BoxStats, ChartAxis, ChartDataset, ChartPoint, ChartValueFormat, ReportChart)


@dataclass(frozen=True)
class ChartRequest:
    type: str
    dimension: str | None = None
    series: str | None = None
    measure: str | None = None
    numeric: tuple[str, ...] = ()
    filters: tuple[tuple[str, str], ...] = ()
    window_days: int | None = None
    granularity: str | None = None
    limit: int | None = None
    sort: str | None = None          # "desc" | "asc" | "natural"
    title: str | None = None
    clause: str = ""
    source: str = "rule"             # "rule" | "ai_plan" | "default"
    resolved: bool = True


class ChartUnavailable(Exception):
    def __init__(self, reason: str, missing: tuple[str, ...] = ()):
        super().__init__(reason)
        self.reason = reason
        self.missing = list(missing)


@dataclass
class BuildContext:
    adapter: DatasetAdapter
    rows: list[dict[str, Any]]
    note: str | None = None          # e.g. the date window, appended to the description


@dataclass(frozen=True)
class ChartSkill:
    type: str
    label: str
    aliases: tuple[str, ...]
    requires: str
    data_shape: str
    max_points: int
    build: Callable[[ChartRequest, BuildContext], ReportChart]
    needs_dimension: bool = False    # an unqualified request ("add a pie chart") is ambiguous
    exportable: bool = True          # False: documents show the data as a table instead

    def describe(self) -> dict[str, Any]:
        return {"type": self.type, "label": self.label, "aliases": [a for a in self.aliases],
                "requires": self.requires, "data_shape": self.data_shape, "max_points": self.max_points,
                "exportable_as_image": self.exportable}


# --- Helpers -------------------------------------------------------------------

def _measure(req: ChartRequest, ctx: BuildContext) -> MeasureSpec:
    return ctx.adapter.measures[req.measure or ctx.adapter.default_measure]


def _field(name: str | None, ctx: BuildContext) -> FieldSpec | None:
    return ctx.adapter.fields.get(name) if name else None


def _dimension(req: ChartRequest, ctx: BuildContext, m: MeasureSpec | None = None) -> FieldSpec:
    name = req.dimension or ctx.adapter.default_dimension.get(req.type)
    # "Cancellations by status" is a single slice: group a status-filtered measure by another field.
    if m and not req.dimension and any(f == name for f, _ in m.where):
        name = next((n for n in ("courier", "payment_mode", "status") if n in ctx.adapter.fields and n != name), name)
    spec = _field(name, ctx)
    if spec is None or spec.kind == "number":
        raise ChartUnavailable(f"No groupable field is available for a {_skill_label(req.type)}.", ("category field",))
    return spec


def _value_format(m: MeasureSpec, unit: str | None = None) -> ChartValueFormat:
    decimals = 1 if m.value_format == "percent" else 2 if m.agg in {"sum", "avg"} and m.value_format == "currency" else \
        2 if m.agg == "avg" else 0
    return ChartValueFormat(type=m.value_format, decimals=decimals, unit=unit or ("kg" if m.field == "weight" else None))


def _axis(spec: FieldSpec | None, label: str, fmt: str = "category") -> ChartAxis:
    return ChartAxis(field=spec.name if spec else None, label=label, unit=spec.unit if spec else None, format=fmt)


def _measure_axis(m: MeasureSpec) -> ChartAxis:
    return ChartAxis(field=m.name, label=m.label, format=m.value_format if m.value_format != "number" else "number",
                     unit="kg" if m.field == "weight" else None)


def _title(req: ChartRequest, default: str, ctx: BuildContext) -> str:
    title = req.title or default
    for f, v in req.filters:
        title += f" ({ctx.adapter.label_of(f, v)})"
    return title[:120]


def _description(ctx: BuildContext, *parts: str | None) -> str | None:
    text = " ".join(p for p in (*parts, ctx.note) if p)
    return text[:300] or None


def _need_rows(ctx: BuildContext, req: ChartRequest) -> None:
    if not ctx.rows:
        raise ChartUnavailable("No records match this chart's scope, so there is nothing to plot.")


def _series(req: ChartRequest, ctx: BuildContext, spec: FieldSpec, m: MeasureSpec, max_points: int, *,
            other_bucket: bool = False):
    """(labels, values, granularity, truncated_total) for one grouping, all computed in Python."""
    if spec.kind == "date":
        gran, buckets = date_buckets(ctx.rows, spec.name, req.granularity, max_points)
        if not buckets:
            raise ChartUnavailable(f"No valid {spec.label.lower()} values in the supplied records.", (spec.name,))
        points = [(label, measure(rows, m) if rows or m.agg in {"count", "sum"} else None) for label, rows in buckets]
        points = [(label, value if value is not None else 0.0) for label, value in points
                  if value is not None or m.agg in {"count", "sum"}]
        return [p[0] for p in points], [p[1] for p in points], gran, 0
    groups = group(ctx.rows, spec.name, ctx.adapter)
    points = [(label, measure(rows, m), rows) for label, rows in groups.items()]
    points = [p for p in points if p[1] is not None]
    if not points:
        raise ChartUnavailable(f"No valid values for {m.label.lower()} in the supplied records.",
                               (m.field,) if m.field else ())
    if req.sort != "natural":
        points.sort(key=lambda p: p[1], reverse=req.sort != "asc")
    limit = min(req.limit or max_points, max_points)
    hidden = len(points) - limit
    if hidden > 0:
        if other_bucket and m.additive:
            rest = [row for p in points[limit - 1:] for row in p[2]]
            points = points[:limit - 1] + [("Other", measure(rest, m), rest)]
        else:
            points = points[:limit]
    return [p[0] for p in points], [p[1] for p in points], None, max(hidden, 0)


def _chart(req: ChartRequest, kind: str, title: str, labels, datasets, **extra) -> ReportChart:
    return ReportChart(id="pending", title=title, type=kind, labels=[str(l)[:120] for l in labels], datasets=datasets,
                       source=req.source, options={"showLegend": True, "showTooltip": True, **extra.pop("options", {})},
                       **extra)


def _gran_word(gran: str | None) -> str:
    return {"day": "Day", "week": "Week", "month": "Month"}.get(gran or "", "")


# --- Builders --------------------------------------------------------------------

def _categorical(kind: str, *, circular: bool = False, other_bucket: bool = False, horizontal: bool = False):
    def build(req: ChartRequest, ctx: BuildContext) -> ReportChart:
        _need_rows(ctx, req)
        skill = SKILLS[kind]
        m = _measure(req, ctx)
        spec = _dimension(req, ctx, m)
        if (circular or kind == "treemap") and not m.additive:
            raise ChartUnavailable(f"A {skill.label.lower()} shows parts of a whole, so it needs a count or total, "
                                   f"not {m.label.lower()}. Use a bar chart for {m.label.lower()}.")
        if circular and spec.kind == "date":
            raise ChartUnavailable(f"A {skill.label.lower()} cannot show a time series; use a line chart for dates.")
        labels, values, gran, hidden = _series(req, ctx, spec, m, skill.max_points, other_bucket=other_bucket)
        if circular or kind == "treemap":
            pairs = [(l, v) for l, v in zip(labels, values) if v > 0]
            labels, values = [p[0] for p in pairs], [p[1] for p in pairs]
            if len(labels) < 2:
                raise ChartUnavailable(f"Only one {spec.label.lower()} has data; a {skill.label.lower()} needs at least two.")
        if kind in {"line", "area"} and len(labels) < 2:
            raise ChartUnavailable(f"A {skill.label.lower()} needs at least two points; the data has only one "
                                   f"{(_gran_word(gran) or spec.label).lower()}.")
        default = (f"{m.label} per {_gran_word(gran)}" if gran else f"{m.label} by {spec.label}")
        title = _title(req, default + (f" (top {len(labels)})" if hidden and not other_bucket else ""), ctx)
        datasets = [ChartDataset(label=m.label, data=values)]
        if spec.name == "courier" and req.clause and re.search(r"\bdeliver", req.clause.lower()) and re.search(r"\b(?:volume|total|shipment|count|orders?)\b", req.clause.lower()):
            deliv_m = ctx.adapter.measures.get("delivered_orders")
            if deliv_m:
                groups = group(ctx.rows, spec.name, ctx.adapter)
                deliv_vals = [float(measure(groups.get(l, []), deliv_m) or 0.0) for l in labels]
                datasets = [
                    ChartDataset(label="Total Volume", data=values),
                    ChartDataset(label="Delivered", data=deliv_vals),
                ]
                title = "Courier Volume vs Delivery Counts"
        elif req.series and kind in {"line", "area"} and spec.kind == "date":
            datasets = _date_series(req, ctx, spec, m, labels, gran)
        fmt = "date" if spec.kind == "date" else "category"
        return _chart(req, kind, title, labels, datasets, x_axis=_axis(spec, spec.label if not gran else _gran_word(gran), fmt),
                      y_axis=_measure_axis(m), value_format=_value_format(m),
                      options={"horizontal": horizontal, "fill": kind == "area", "granularity": gran} if not circular else
                      {"innerRadius": "55%" if kind == "doughnut" else "0%"},
                      description=_description(ctx, f"{hidden} smaller groups are not shown." if hidden and not other_bucket else None))
    return build


def _date_series(req, ctx, spec, m, labels, gran):
    series = _field(req.series, ctx)
    if series is None or series.kind != "category":
        raise ChartUnavailable("A multi-series line chart needs a category field for its series.", ("series field",))
    groups = group(ctx.rows, series.name, ctx.adapter)
    top = sorted(groups.items(), key=lambda g: len(g[1]), reverse=True)[:6]
    datasets = []
    _, buckets = date_buckets(ctx.rows, spec.name, gran, len(labels))
    for name, rows in top:
        ids = {id(r) for r in rows}
        by_label = {label: [r for r in bucket if id(r) in ids] for label, bucket in buckets}
        datasets.append(ChartDataset(label=name, data=[measure(by_label.get(l, []), m) or 0.0 for l in labels]))
    return datasets


def _matrix(kind: str):
    """Grouped / stacked bars and heatmaps: two dimensions, one measure per cell."""
    def build(req: ChartRequest, ctx: BuildContext) -> ReportChart:
        _need_rows(ctx, req)
        skill = SKILLS[kind]
        m = _measure(req, ctx)
        x = _dimension(req, ctx)
        status = ctx.adapter.status_field
        y_name = req.series or (status if x.name != status else "payment_mode" if "payment_mode" in ctx.adapter.fields else None)
        if y_name == x.name:
            y_name = None
        y = _field(y_name, ctx)
        if y is None or y.kind == "number":
            raise ChartUnavailable(f"A {skill.label.lower()} needs two category fields; only {x.label.lower()} was available.",
                                   ("second category field",))
        if kind == "stacked_bar" and not m.additive:
            raise ChartUnavailable(f"Stacked bars add segments together, so they need a count or total, not {m.label.lower()}.")
        share = m.agg == "rate" and y.name == status  # "delivery performance" heatmap: % of each row's orders per status
        if m.agg == "rate" and y.name == status and kind != "heatmap":
            m = ctx.adapter.measures[ctx.adapter.default_measure]
        x_groups = group(ctx.rows, x.name, ctx.adapter)
        y_groups = group(ctx.rows, y.name, ctx.adapter)
        x_max, y_max = (15, 10) if kind == "heatmap" else (12, 6)
        x_keys = [k for k, _ in sorted(x_groups.items(), key=lambda g: len(g[1]), reverse=True)][:x_max]
        y_keys = [k for k, _ in sorted(y_groups.items(), key=lambda g: len(g[1]), reverse=True)][:y_max]
        if len(x_keys) < (2 if kind == "heatmap" else 1) or len(y_keys) < 2:
            raise ChartUnavailable(f"A {skill.label.lower()} needs at least two {x.label.lower()} and two "
                                   f"{y.label.lower()} values; the data does not have enough.")
        if share:
            cell = lambda xr, yr: round(100.0 * len(yr) / len(xr), 1) if xr else 0.0  # noqa: E731
        else:
            cell = lambda xr, yr: measure(yr, m) or 0.0  # noqa: E731
        datasets = []
        for yk in y_keys:
            data = []
            for xk in x_keys:
                xr = x_groups[xk]
                data.append(cell(xr, [r for r in xr if ctx.adapter.label_of(y.name, r.get(y.name)) == yk]))
            datasets.append(ChartDataset(label=yk, data=data))
        label = "Share of Orders (%)" if share else m.label
        fmt = ChartValueFormat(type="percent", decimals=1) if share else _value_format(m)
        hidden = len(x_groups) - len(x_keys)
        return _chart(req, kind, _title(req, f"{label} by {x.label} and {y.label}", ctx), x_keys, datasets,
                      x_axis=_axis(x, x.label), y_axis=_axis(y, y.label) if kind == "heatmap" else
                      ChartAxis(field=m.name, label=label, format=fmt.type), value_format=fmt,
                      options={"stacked": kind == "stacked_bar", "series_field": y.name,
                               **({"min": 0, "max": max((v for d in datasets for v in d.data), default=0)} if kind == "heatmap" else {})},
                      description=_description(ctx, "Each cell is the share of that row's orders in each status." if share else None,
                                               f"{hidden} smaller {x.label.lower()} groups are not shown." if hidden > 0 else None))
    return build


def _numeric_field(req: ChartRequest, ctx: BuildContext, minimum: int, index: int = 0) -> tuple[FieldSpec, list[float]]:
    m = ctx.adapter.measures.get(req.measure or "")
    candidates = list(req.numeric[index:index + 1]) or ([m.field] if m and m.field and index == 0 else []) or \
        [f for f in ctx.adapter.numeric_defaults if f not in req.numeric[:index]]
    explicit = bool(req.numeric[index:index + 1]) or bool(m and m.field and index == 0)
    for name in candidates:
        spec = _field(name, ctx)
        values = [v for v in (valid_number(r.get(name)) for r in ctx.rows) if v is not None] if spec else []
        if spec and len(values) >= minimum:
            return spec, values
        if explicit:
            raise ChartUnavailable(f"{spec.label if spec else name} has fewer than {minimum} valid numeric values "
                                   "(null, text, negative or non-finite values are excluded).", (name,))
    raise ChartUnavailable(f"No numeric field has at least {minimum} valid values.", ctx.adapter.numeric_defaults)


def _histogram(req: ChartRequest, ctx: BuildContext) -> ReportChart:
    _need_rows(ctx, req)
    spec, values = _numeric_field(req, ctx, 5)
    bins = histogram(values, SKILLS["histogram"].max_points)
    unit = f" ({spec.unit})" if spec.unit else ""
    return _chart(req, "histogram", _title(req, f"Distribution of {spec.label}", ctx), [b[0] for b in bins],
                  [ChartDataset(label=ctx.adapter.record_label, data=[float(b[1]) for b in bins])],
                  x_axis=_axis(spec, spec.label + unit, "number"), y_axis=ChartAxis(label=ctx.adapter.record_label, format="number"),
                  value_format=ChartValueFormat(type="number"), options={"bins": len(bins), "binFormat": spec.value_format},
                  description=_description(ctx, f"{len(values)} records with a valid {spec.label.lower()}."))


def _boxplot(req: ChartRequest, ctx: BuildContext) -> ReportChart:
    _need_rows(ctx, req)
    spec, _ = _numeric_field(req, ctx, 3)
    dim = _dimension(req, ctx)
    stats = []
    for label, rows in group(ctx.rows, dim.name, ctx.adapter).items():
        values = [v for v in (valid_number(r.get(spec.name)) for r in rows) if v is not None]
        if len(values) >= 3:
            stats.append((label, quartiles(values)))
    stats = sorted(stats, key=lambda s: s[1]["count"], reverse=True)[:SKILLS["boxplot"].max_points]
    if not stats:
        raise ChartUnavailable(f"A box plot needs at least three valid {spec.label.lower()} values in one "
                               f"{dim.label.lower()}.", (spec.name,))
    fmt = ChartValueFormat(type=spec.value_format, decimals=2, unit=spec.unit)
    return _chart(req, "boxplot", _title(req, f"{spec.label} by {dim.label}", ctx), [s[0] for s in stats],
                  [ChartDataset(label=spec.label, data=[s[1]["median"] for s in stats], boxes=[BoxStats(**s[1]) for s in stats])],
                  x_axis=_axis(dim, dim.label), y_axis=_axis(spec, spec.label, spec.value_format), value_format=fmt,
                  description=_description(ctx, "Groups with fewer than three valid values are omitted."))


def _scatter(req: ChartRequest, ctx: BuildContext) -> ReportChart:
    _need_rows(ctx, req)
    x, _ = _numeric_field(req, ctx, 3, 0)
    y, _ = _numeric_field(replace(req, numeric=(x.name, *req.numeric[1:2])), ctx, 3, 1)
    pairs = [(valid_number(r.get(x.name)), valid_number(r.get(y.name))) for r in ctx.rows]
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    if len(pairs) < 3:
        raise ChartUnavailable(f"Fewer than three records have both a valid {x.label.lower()} and {y.label.lower()}.",
                               (x.name, y.name))
    shown = pairs[:SKILLS["scatter"].max_points]
    return _chart(req, "scatter", _title(req, f"{y.label} vs {x.label}", ctx), [ctx.adapter.record_label],
                  [ChartDataset(label=ctx.adapter.record_label, data=[], points=[ChartPoint(x=a, y=b) for a, b in shown])],
                  x_axis=_axis(x, x.label, x.value_format), y_axis=_axis(y, y.label, y.value_format),
                  value_format=ChartValueFormat(type=y.value_format, decimals=2, unit=y.unit),
                  description=_description(ctx, f"Showing {len(shown)} of {len(pairs)} records with both values."
                                           if len(shown) < len(pairs) else f"{len(pairs)} records with both values."))


def _bubble(req: ChartRequest, ctx: BuildContext) -> ReportChart:
    _need_rows(ctx, req)
    x, _ = _numeric_field(req, ctx, 2, 0)
    y, _ = _numeric_field(replace(req, numeric=(x.name, *req.numeric[1:2])), ctx, 2, 1)
    dim = _dimension(req, ctx)
    points = []
    for label, rows in group(ctx.rows, dim.name, ctx.adapter).items():
        xs = [v for v in (valid_number(r.get(x.name)) for r in rows) if v is not None]
        ys = [v for v in (valid_number(r.get(y.name)) for r in rows) if v is not None]
        if xs and ys:
            points.append((label, round(sum(xs) / len(xs), 2), round(sum(ys) / len(ys), 2), len(rows)))
    points = sorted(points, key=lambda p: p[3], reverse=True)[:SKILLS["bubble"].max_points]
    if len(points) < 2:
        raise ChartUnavailable(f"A bubble chart needs at least two {dim.label.lower()} groups with valid "
                               f"{x.label.lower()} and {y.label.lower()} values.", (x.name, y.name))
    return _chart(req, "bubble", _title(req, f"Average {y.label} vs {x.label} by {dim.label}", ctx), [p[0] for p in points],
                  [ChartDataset(label=dim.label, data=[], points=[ChartPoint(x=p[1], y=p[2], r=p[3], label=p[0]) for p in points])],
                  x_axis=_axis(x, "Average " + x.label, x.value_format), y_axis=_axis(y, "Average " + y.label, y.value_format),
                  value_format=ChartValueFormat(type=y.value_format, decimals=2, unit=y.unit),
                  options={"sizeField": "record_count"},
                  description=_description(ctx, f"Bubble size is the number of {ctx.adapter.record_label.lower()}."))


def _funnel(req: ChartRequest, ctx: BuildContext) -> ReportChart:
    _need_rows(ctx, req)
    if req.dimension or not ctx.adapter.funnel:
        return _categorical("funnel")(replace(req, sort="desc"), ctx)
    labels, values = [], []
    for stage in ctx.adapter.funnel:
        rows = [r for r in ctx.rows if matches(r, stage.where) and not (stage.exclude and matches(r, stage.exclude))]
        labels.append(stage.label)
        values.append(float(len(rows)))
    return _chart(req, "funnel", _title(req, "Shipment Lifecycle Funnel", ctx), labels,
                  [ChartDataset(label=ctx.adapter.record_label, data=values)],
                  y_axis=ChartAxis(label=ctx.adapter.record_label, format="number"), value_format=ChartValueFormat(),
                  options={"sort": "none"}, description=_description(ctx, "Each stage counts orders that reached it."))


def _waterfall(req: ChartRequest, ctx: BuildContext) -> ReportChart:
    _need_rows(ctx, req)
    a = ctx.adapter
    if not a.waterfall_steps or not a.status_field:
        raise ChartUnavailable("A waterfall chart needs a defined sequence of steps, which this report type does not have.")
    counts = Counter(r.get(a.status_field) for r in ctx.rows)
    final = a.waterfall_steps[-1]
    labels, values, steps = [a.waterfall_total or "Total"], [float(len(ctx.rows))], ["total"]
    for step in a.waterfall_steps[:-1]:
        if counts.get(step):
            labels.append(a.label_of(a.status_field, step))
            values.append(float(counts[step]))
            steps.append("decrease")
    labels.append(a.label_of(a.status_field, final))
    values.append(float(counts.get(final, 0)))
    steps.append("total")
    return _chart(req, "waterfall", _title(req, "From Total Orders to Delivered", ctx), labels,
                  [ChartDataset(label=a.record_label, data=values)], y_axis=ChartAxis(label=a.record_label, format="number"),
                  value_format=ChartValueFormat(), options={"steps": steps},
                  description=_description(ctx, "Each decrease is the orders in that status; the last bar is what remains."))


def _gauge(req: ChartRequest, ctx: BuildContext) -> ReportChart:
    _need_rows(ctx, req)
    a = ctx.adapter
    name = req.measure or (a.rate_measures[0] if a.rate_measures else None)
    m = a.measures.get(name or "")
    if m is None or m.agg != "rate":
        raise ChartUnavailable("A gauge shows a single percentage; ask for a rate such as delivery rate or cancellation rate.",
                               ("percentage measure",))
    value = measure(ctx.rows, m)
    return _chart(req, "gauge", _title(req, m.label, ctx), [m.label], [ChartDataset(label=m.label, data=[value])],
                  value_format=_value_format(m), options={"min": 0, "max": 100},
                  description=_description(ctx, f"Share of {len(ctx.rows)} {a.record_label.lower()}."))


def _radar(req: ChartRequest, ctx: BuildContext) -> ReportChart:
    _need_rows(ctx, req)
    a = ctx.adapter
    axes = [a.measures[n] for n in a.rate_measures]
    if len(axes) < 3:
        raise ChartUnavailable("A radar chart needs at least three comparable percentage measures.", ("percentage measures",))
    dim = _dimension(req, ctx)
    groups = sorted(group(ctx.rows, dim.name, a).items(), key=lambda g: len(g[1]), reverse=True)
    groups = groups[:min(req.limit or 5, 5)]
    return _chart(req, "radar", _title(req, f"{dim.label} Performance Profile", ctx), [m.label for m in axes],
                  [ChartDataset(label=name, data=[measure(rows, m) for m in axes]) for name, rows in groups],
                  x_axis=_axis(dim, dim.label), value_format=ChartValueFormat(type="percent", decimals=1),
                  options={"min": 0, "max": 100},
                  description=_description(ctx, f"The {len(groups)} largest {dim.label.lower()} groups by order count."))


def _map(req: ChartRequest, ctx: BuildContext) -> ReportChart:
    _need_rows(ctx, req)
    a = ctx.adapter
    if not a.geo_field:
        raise ChartUnavailable("This report type has no geographic field, so a map cannot be drawn.", ("geographic field",))
    m = _measure(req, ctx)
    named = [r for r in ctx.rows if r.get(a.geo_field)]
    known = [(canonical_state(r[a.geo_field]), r) for r in named]
    known = [(state, r) for state, r in known if state]
    if not named or len(known) / len(named) < 0.6:
        raise ChartUnavailable("Destination states are missing or not recognisable as Indian states/UTs, so a map "
                               "would be misleading.", (a.geo_field,))
    groups: dict[str, list] = {}
    for state, row in known:
        groups.setdefault(state, []).append(row)
    points = sorted(((s, measure(rows, m)) for s, rows in groups.items()), key=lambda p: p[1] or 0, reverse=True)
    points = [p for p in points if p[1] is not None]
    unmatched = len(named) - len(known)
    return _chart(req, "map", _title(req, f"{m.label} by State", ctx), [p[0] for p in points],
                  [ChartDataset(label=m.label, data=[p[1] for p in points])],
                  x_axis=ChartAxis(field=a.geo_field, label="State"), y_axis=_measure_axis(m), value_format=_value_format(m),
                  options={"map": "india", "regionKey": "name"},
                  description=_description(ctx, f"{unmatched} records with an unrecognised state are excluded." if unmatched else None))


SKILLS: dict[str, ChartSkill] = {}


def register(skill: ChartSkill) -> ChartSkill:
    SKILLS[skill.type] = skill
    return skill


def _skill_label(kind: str) -> str:
    return SKILLS[kind].label.lower() if kind in SKILLS else kind


_CHART = r"(?:\s*-?\s*(?:charts?|graphs?|plots?|diagrams?|visuals?))"
# Registration order is matching priority: specific phrases ("stacked bar") before generic ones ("bar").
for _skill in (
    ChartSkill("horizontal_bar", "Horizontal bar chart", (rf"horizontal\s+(?:bar|column)s?{_CHART}?", r"\bh-?bar\b"),
               "one category field and one measure", "categories -> one value each", 25,
               _categorical("horizontal_bar", horizontal=True), needs_dimension=True),
    ChartSkill("stacked_bar", "Stacked bar chart", (rf"stacked\s+(?:bar|column)s?{_CHART}?", rf"stacked{_CHART}"),
               "two category fields and an additive measure", "categories x series matrix", 12, _matrix("stacked_bar"),
               needs_dimension=True),
    ChartSkill("grouped_bar", "Grouped bar chart",
               (rf"(?:grouped|clustered|side[- ]by[- ]side|multi[- ]series)\s+(?:bar|column)s?{_CHART}?",),
               "two category fields and a measure", "categories x series matrix", 12, _matrix("grouped_bar"),
               needs_dimension=True),
    ChartSkill("heatmap", "Heatmap", (r"\bheat\s*-?\s*maps?\b",), "two category fields and a measure",
               "x categories x y categories matrix", 15, _matrix("heatmap")),
    ChartSkill("treemap", "Treemap", (r"\btree\s*-?\s*maps?\b",), "one category field and an additive measure",
               "categories -> one value each", 30, _categorical("treemap", other_bucket=True), needs_dimension=True),
    ChartSkill("boxplot", "Box plot", (r"\bbox\s*(?:-?\s*and\s*-?\s*whiskers?|\s*plots?|\s*charts?)", r"\bboxplots?\b",
                                       r"\bwhisker\s+plots?"),
               "one numeric field, optionally grouped by a category", "five-number summary per group", 12, _boxplot),
    ChartSkill("histogram", "Histogram", (r"\bhistograms?\b", r"\bfrequency\s+(?:distribution|chart)s?"),
               "one numeric field with at least 5 valid values", "equal-width bins -> record count", 12, _histogram),
    ChartSkill("scatter", "Scatter plot", (rf"\bscatter{_CHART}?", r"\bscatterplots?\b", rf"\bcorrelation{_CHART}"),
               "two numeric fields on the same records", "points (x, y)", 500, _scatter),
    ChartSkill("bubble", "Bubble chart", (rf"\bbubble{_CHART}?",), "two numeric fields and a category field",
               "one point (x, y, size) per category", 20, _bubble),
    ChartSkill("waterfall", "Waterfall chart", (rf"\b(?:waterfall|bridge|cascade){_CHART}", r"\bwaterfall\b"),
               "a status field with a defined sequence", "total -> decreases -> remaining total", 12, _waterfall),
    ChartSkill("funnel", "Funnel chart", (r"\bfunnels?\b",), "lifecycle stages or one category field",
               "ordered stages -> count", 8, _funnel),
    ChartSkill("radar", "Radar chart", (rf"\b(?:radar|spider|web|polar){_CHART}", r"\bradar\b"),
               "at least three percentage measures and a category field", "metrics x groups", 6, _radar),
    ChartSkill("gauge", "Gauge chart", (r"\bgauges?\b", r"\bspeedometers?\b", rf"\b(?:meter|dial){_CHART}"),
               "one percentage measure", "single value 0-100", 1, _gauge),
    ChartSkill("map", "Geographic map", (r"\bchoropleth\b", r"\bgeo(?:graphic(?:al)?)?\s*maps?\b", r"\b(?:india|state)\s+maps?\b",
                                          r"\bmaps?\b"),
               "a geographic field with recognisable Indian states/UTs", "region -> value", 40, _map, exportable=False),
    ChartSkill("doughnut", "Doughnut chart", (rf"\b(?:donut|doughnut)s?{_CHART}?", rf"\bring{_CHART}"),
               "one category field and an additive measure", "2-12 categories -> share", 12,
               _categorical("doughnut", circular=True, other_bucket=True), needs_dimension=True),
    ChartSkill("pie", "Pie chart", (rf"\bpie{_CHART}?",), "one category field and an additive measure",
               "2-12 categories -> share", 12, _categorical("pie", circular=True, other_bucket=True), needs_dimension=True),
    ChartSkill("area", "Area chart", (rf"\barea{_CHART}",), "a date field (or category) and a measure",
               "ordered points -> value", 62, _categorical("area")),
    ChartSkill("line", "Line chart", (rf"\bline{_CHART}", r"\blines?\s+graphs?", rf"\btrend\s*lines?{_CHART}?"),
               "a date field (or category) and a measure", "ordered points -> value", 62, _categorical("line")),
    ChartSkill("bar", "Bar chart", (rf"\b(?:bar|column|baar)s?{_CHART}", r"\bbars\b"),
               "one category or date field and one measure", "categories -> one value each", 20, _categorical("bar"),
               needs_dimension=True),
):
    register(_skill)

ALIASES: dict[str, str] = {"donut": "doughnut", "column": "bar", "hbar": "horizontal_bar", "box": "boxplot",
                           "box_plot": "boxplot", "heat_map": "heatmap", "tree_map": "treemap", "spider": "radar",
                           "geo": "map", "choropleth": "map", "speedometer": "gauge"}


def resolve_type(name: str) -> str | None:
    key = re.sub(r"[\s-]+", "_", str(name or "").strip().lower())
    key = ALIASES.get(key, key)
    return key if key in SKILLS else None
