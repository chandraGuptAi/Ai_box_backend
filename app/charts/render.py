"""Static chart rendering for PDF/Word exports (no browser needed) and a data-table fallback.

The six legacy types keep the existing renderer's look. Other types use matplotlib here. A type that
cannot be drawn faithfully as a static image (the geographic map) returns None, and exporters show
``chart_table`` with ``FALLBACK_NOTE`` instead.
"""
from __future__ import annotations

import logging
import math
import threading
from io import BytesIO

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
import matplotlib.patches as patches

from app.generators.chart_renderer import _COLORS, chart_png

logger = logging.getLogger(__name__)
_LOCK = threading.Lock()
_LEGACY = {"bar", "line", "area", "pie", "doughnut", "donut"}
FALLBACK_NOTE = "This chart type cannot be drawn in a document, so its data is shown as a table."


def render_png(chart) -> bytes | None:
    """PNG bytes for a chart, or None when the exporter should use the table fallback."""
    kind = chart.type
    if kind == "map":
        return None
    try:
        if kind in _LEGACY and len(chart.datasets) == 1 and not chart.options.get("horizontal"):
            return chart_png(chart)
        with _LOCK:
            return _render(chart)
    except Exception:
        logger.exception("Chart %s (%s) could not be rendered; using a table.", chart.id, kind)
        return None


def chart_table(chart) -> tuple[list[str], list[list[str]]]:
    """Exact chart data as table columns and rows (fallback for documents)."""
    fmt = _formatter(chart)
    first = chart.x_axis.label if chart.x_axis else "Category"
    if chart.type in {"scatter", "bubble"}:
        x = chart.x_axis.label if chart.x_axis else "X"
        y = chart.y_axis.label if chart.y_axis else "Y"
        cols = ["Group", x, y] + (["Records"] if chart.type == "bubble" else [])
        rows = [[p.label or d.label, _plain(p.x), fmt(p.y)] + ([_plain(p.r)] if chart.type == "bubble" else [])
                for d in chart.datasets for p in (d.points or [])]
        return cols, rows
    if any(d.boxes for d in chart.datasets):
        d = chart.datasets[0]
        return [first, "Min", "Q1", "Median", "Q3", "Max", "Count"], [
            [label, fmt(b.min), fmt(b.q1), fmt(b.median), fmt(b.q3), fmt(b.max), str(b.count)]
            for label, b in zip(chart.labels, d.boxes)]
    cols = [first] + [d.label for d in chart.datasets]
    rows = [[label] + [fmt(d.data[i]) for d in chart.datasets] for i, label in enumerate(chart.labels)]
    return cols, rows


def _plain(value) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def _formatter(chart):
    vf = chart.value_format
    kind, decimals = (vf.type, vf.decimals) if vf else ("number", None)

    def fmt(value) -> str:
        if value is None:
            return ""
        if kind == "percent":
            return f"{value:.{decimals if decimals is not None else 1}f}%"
        if kind == "currency" or (decimals and not float(value).is_integer()):
            return f"{value:,.{decimals or 2}f}"
        return _plain(value)
    return fmt


def _figure(polar: bool = False):
    figure = Figure(figsize=(7.5, 3.8), dpi=180, facecolor="#ffffff")
    FigureCanvasAgg(figure)
    ax = figure.add_subplot(111, polar=polar)
    return figure, ax


def _style(ax, chart) -> None:
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color("#cbd5e1")
    ax.spines["bottom"].set_color("#cbd5e1")
    ax.tick_params(colors="#475569", labelsize=7)
    if chart.x_axis:
        ax.set_xlabel(chart.x_axis.label, fontsize=8, color="#475569")
    if chart.y_axis:
        ax.set_ylabel(chart.y_axis.label, fontsize=8, color="#475569")


def _short(label: str, n: int = 14) -> str:
    return label if len(label) <= n else label[:n - 1] + "…"


def _png(figure) -> bytes:
    buffer = BytesIO()
    figure.tight_layout()
    figure.savefig(buffer, format="png", facecolor="#ffffff")
    return buffer.getvalue()


def _render(chart) -> bytes:
    kind, labels, sets = chart.type, [str(l) for l in chart.labels], chart.datasets
    fmt = _formatter(chart)
    figure, ax = _figure(polar=kind == "radar")
    if kind in {"bar", "horizontal_bar", "grouped_bar", "stacked_bar", "histogram"}:
        horizontal = kind == "horizontal_bar" or chart.options.get("horizontal")
        n, width = len(sets), (0.8 / len(sets) if kind == "grouped_bar" else 0.95 if kind == "histogram" else 0.7)
        bottoms = [0.0] * len(labels)
        for s, d in enumerate(sets):
            pos = [i + (s - (n - 1) / 2) * width if kind == "grouped_bar" else i for i in range(len(labels))]
            color = _COLORS[s % len(_COLORS)]
            if horizontal:
                ax.barh(pos, d.data, height=width, color=color, label=d.label)
            else:
                ax.bar(pos, d.data, width=width, bottom=bottoms if kind == "stacked_bar" else None, color=color, label=d.label)
            if kind == "stacked_bar":
                bottoms = [b + v for b, v in zip(bottoms, d.data)]
        if horizontal:
            ax.set_yticks(range(len(labels)), [_short(l, 22) for l in labels])
            ax.invert_yaxis()
        else:
            ax.set_xticks(range(len(labels)), [_short(l) for l in labels], rotation=30 if len(labels) > 6 else 0, ha="right" if len(labels) > 6 else "center")
        _style(ax, chart)
        if n > 1:
            ax.legend(fontsize=7, frameon=False, loc="upper left", bbox_to_anchor=(1.0, 1.0))
    elif kind in {"line", "area"}:
        for s, d in enumerate(sets):
            color = _COLORS[s % len(_COLORS)]
            if kind == "area":
                ax.fill_between(range(len(labels)), d.data, alpha=0.42, color=color, label=d.label)
                ax.plot(range(len(labels)), d.data, marker="o", markersize=3.5, color=color)
            else:
                ax.plot(range(len(labels)), d.data, marker="o", markersize=3.5, color=color, label=d.label)
        step = max(1, len(labels) // 10)
        ax.set_xticks(range(0, len(labels), step), [_short(labels[i]) for i in range(0, len(labels), step)], rotation=30, ha="right")
        _style(ax, chart)
        ax.legend(fontsize=7, frameon=False)
    elif kind in {"pie", "doughnut", "donut"}:
        d = sets[0]
        ax.pie(d.data, labels=[_short(l) for l in labels], colors=_COLORS, autopct="%1.1f%%", textprops={"fontsize": 7},
               wedgeprops={"width": 0.45} if kind != "pie" else None)
        ax.set_aspect("equal")
    elif kind == "heatmap":
        matrix = [d.data for d in sets]
        image = ax.imshow(matrix, cmap="Blues", aspect="auto")
        ax.set_xticks(range(len(labels)), [_short(l) for l in labels], rotation=30, ha="right")
        ax.set_yticks(range(len(sets)), [_short(d.label, 18) for d in sets])
        peak = max((v for row in matrix for v in row), default=0) or 1
        for y, row in enumerate(matrix):
            for x, v in enumerate(row):
                ax.text(x, y, fmt(v), ha="center", va="center", fontsize=6, color="white" if v > peak * 0.6 else "#1e293b")
        figure.colorbar(image, ax=ax, fraction=0.03)
        _style(ax, chart)
    elif kind == "boxplot":
        d = sets[0]
        ax.bxp([{"label": _short(l), "whislo": b.min, "q1": b.q1, "med": b.median, "q3": b.q3, "whishi": b.max, "fliers": []}
                for l, b in zip(labels, d.boxes)], showfliers=False, patch_artist=True,
               boxprops={"facecolor": "#c7d2fe", "edgecolor": _COLORS[0]}, medianprops={"color": _COLORS[2]})
        _style(ax, chart)
    elif kind in {"scatter", "bubble"}:
        d = sets[0]
        xs, ys = [p.x for p in d.points], [p.y for p in d.points]
        if kind == "bubble":
            peak = max((p.r or 1) for p in d.points)
            ax.scatter(xs, ys, s=[60 + 900 * (p.r or 1) / peak for p in d.points], alpha=0.55, color=_COLORS[:len(xs)])
            for p in d.points:
                ax.annotate(_short(p.label or ""), (p.x, p.y), fontsize=6, ha="center")
        else:
            ax.scatter(xs, ys, s=14, alpha=0.7, color=_COLORS[0])
        _style(ax, chart)
    elif kind == "funnel":
        d = sets[0]
        peak = max(d.data) or 1
        for i, v in enumerate(d.data):
            ax.barh(i, v, left=(peak - v) / 2, color=_COLORS[i % len(_COLORS)], height=0.75)
            ax.text(peak / 2, i, f"{labels[i]}: {fmt(v)}", ha="center", va="center", fontsize=7, color="#0f172a")
        ax.invert_yaxis()
        ax.axis("off")
    elif kind == "waterfall":
        d, steps = sets[0], chart.options.get("steps") or []
        running = 0.0
        for i, v in enumerate(d.data):
            step = steps[i] if i < len(steps) else "total"
            if step == "total":
                ax.bar(i, v, color=_COLORS[0])
                running = v
            else:
                running -= v
                ax.bar(i, v, bottom=running, color=_COLORS[2])
            ax.text(i, (v if step == "total" else running + v) , fmt(v), ha="center", va="bottom", fontsize=6)
        ax.set_xticks(range(len(labels)), [_short(l) for l in labels], rotation=30, ha="right")
        _style(ax, chart)
    elif kind == "treemap":
        ax.axis("off")
        items = sorted(zip(labels, sets[0].data), key=lambda p: p[1], reverse=True)
        for i, (label, value, (x, y, w, h)) in enumerate(_treemap(items, 0, 0, 1, 1)):
            ax.add_patch(patches.Rectangle((x, y), w, h, facecolor=_COLORS[i % len(_COLORS)], edgecolor="white", linewidth=1.5))
            if w * h > 0.02:
                ax.text(x + w / 2, y + h / 2, f"{_short(label)}\n{fmt(value)}", ha="center", va="center", fontsize=6, color="white")
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
    elif kind == "radar":
        angles = [2 * math.pi * i / len(labels) for i in range(len(labels))]
        for s, d in enumerate(sets):
            color = _COLORS[s % len(_COLORS)]
            ax.plot(angles + angles[:1], d.data + d.data[:1], color=color, label=d.label)
            ax.fill(angles + angles[:1], d.data + d.data[:1], color=color, alpha=0.12)
        ax.set_xticks(angles, [_short(l, 18) for l in labels], fontsize=7)
        ax.set_ylim(chart.options.get("min", 0), chart.options.get("max", None) or None)
        ax.tick_params(labelsize=6)
        ax.legend(fontsize=6, frameon=False, loc="upper right", bbox_to_anchor=(1.35, 1.1))
    elif kind == "gauge":
        value = sets[0].data[0]
        low, high = chart.options.get("min", 0), chart.options.get("max", 100) or 100
        share = max(0.0, min(1.0, (value - low) / (high - low)))
        ax.add_patch(patches.Wedge((0, 0), 1, 0, 180, width=0.3, facecolor="#e2e8f0"))
        ax.add_patch(patches.Wedge((0, 0), 1, 180 - 180 * share, 180, width=0.3, facecolor=_COLORS[1]))
        ax.text(0, 0.1, fmt(value), ha="center", va="center", fontsize=16, color="#0f172a")
        ax.text(0, -0.15, labels[0], ha="center", va="center", fontsize=8, color="#475569")
        ax.set_xlim(-1.1, 1.1)
        ax.set_ylim(-0.3, 1.1)
        ax.set_aspect("equal")
        ax.axis("off")
    else:
        raise ValueError(f"No static renderer for chart type {kind}")
    return _png(figure)


def _treemap(items, x, y, w, h):
    """Binary-split treemap: areas are proportional to the values."""
    if not items:
        return []
    if len(items) == 1:
        return [(items[0][0], items[0][1], (x, y, w, h))]
    total = sum(v for _, v in items) or 1
    running, cut = 0.0, 1
    for i, (_, v) in enumerate(items[:-1], 1):
        running += v
        cut = i
        if running >= total / 2:
            break
    left, right = items[:cut], items[cut:]
    share = sum(v for _, v in left) / total
    if w >= h:
        return _treemap(left, x, y, w * share, h) + _treemap(right, x + w * share, y, w * (1 - share), h)
    return _treemap(left, x, y, w, h * share) + _treemap(right, x, y + h * share, w, h * (1 - share))
