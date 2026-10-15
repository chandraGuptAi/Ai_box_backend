"""Render the report chart contract identically in PDF and Word, in memory."""
from io import BytesIO
import threading
import textwrap

import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg

import math
import matplotlib.patches as patches

_LOCK = threading.Lock()
# Match prototype & frontend palette (CHART_COLORS in chart.component.ts)
_COLORS = [
    "#5b5fe9", "#16a34a", "#ef4444", "#f59e0b",
    "#9aa1b2", "#3b82f6", "#8b5cf6", "#14b8a6"
]


def chart_png(chart) -> bytes:
    """Render a report chart into crisp PNG bytes for PDF & DOCX embedding."""
    with _LOCK:
        chart_type = (chart.type or "").lower()
        is_circular = chart_type in {"pie", "doughnut", "donut"}
        labels = [str(lbl).strip() for lbl in chart.labels]

        if is_circular and len(chart.datasets) >= 1:
            figure = Figure(figsize=(7.5, 3.8), dpi=180, facecolor="#ffffff")
            FigureCanvasAgg(figure)
            values = chart.datasets[0].data
            total = sum(v for v in values if isinstance(v, (int, float)))

            if total > 0 and len(labels) == len(values):
                ax = figure.add_axes([0.02, 0.10, 0.96, 0.88])
                ax.set_aspect("equal")
                ax.axis("off")
                ax.set_xlim(-2.5, 2.5)
                ax.set_ylim(-1.5, 1.5)

                positive_vals = [v for v in values if v > 0]
                min_v = min(positive_vals) if positive_vals else 0
                max_v = max(positive_vals) if positive_vals else 0
                is_donut = chart_type in {"doughnut", "donut"}
                r_in = 0.38 if is_donut else 0.0
                r_min = 0.72
                r_max = 1.05

                colors = [_COLORS[i % len(_COLORS)] for i in range(len(values))]
                cur_angle = 90.0  # Start at top
                raw_callouts = []

                for i, (v, lbl, col) in enumerate(zip(values, labels, colors)):
                    if not v or v <= 0:
                        continue
                    slice_deg = (v / total) * 360.0
                    # Variable radius: higher volume slices extend outward
                    if not is_donut or max_v == min_v:
                        r_out = 0.95
                    else:
                        r_out = r_min + ((v - min_v) / (max_v - min_v or 1)) * (r_max - r_min)

                    theta1 = cur_angle - slice_deg
                    theta2 = cur_angle
                    wedge = patches.Wedge(
                        center=(0, 0), r=r_out, theta1=theta1, theta2=theta2,
                        width=r_out - r_in, facecolor=col, edgecolor="white", linewidth=1.5
                    )
                    ax.add_patch(wedge)

                    mid_deg = (theta1 + theta2) / 2.0
                    mid_rad = math.radians(mid_deg)
                    pct_val = (v / total) * 100

                    # Candidate pointer callouts for significant slices
                    if (v / total) >= (0.015 if len(values) <= 4 else 0.03):
                        is_right = math.cos(mid_rad) >= 0
                        x1 = (r_out + 0.02) * math.cos(mid_rad)
                        y1 = (r_out + 0.02) * math.sin(mid_rad)
                        orig_x2 = (r_out + 0.18) * math.cos(mid_rad)
                        orig_y2 = (r_out + 0.18) * math.sin(mid_rad)
                        raw_callouts.append({
                            "mid_deg": mid_deg, "is_right": is_right,
                            "x1": x1, "y1": y1, "orig_x2": orig_x2, "orig_y2": orig_y2,
                            "target_y": orig_y2, "label": lbl, "pct": f"{pct_val:.1f}%",
                            "val": v, "color": col
                        })
                    cur_angle -= slice_deg

                # Anti-collision on left and right sides
                def resolve_side(items):
                    items.sort(key=lambda x: x["orig_y2"])
                    min_gap = 0.22
                    for k in range(1, len(items)):
                        if items[k]["target_y"] < items[k - 1]["target_y"] + min_gap:
                            items[k]["target_y"] = items[k - 1]["target_y"] + min_gap
                    max_b = 1.30
                    if items and items[-1]["target_y"] > max_b:
                        overflow = items[-1]["target_y"] - max_b
                        for k in range(len(items) - 1, -1, -1):
                            items[k]["target_y"] = max(-1.30, items[k]["target_y"] - overflow)
                            if k > 0 and items[k - 1]["target_y"] > items[k]["target_y"] - min_gap:
                                items[k - 1]["target_y"] = items[k]["target_y"] - min_gap

                right_items = [c for c in raw_callouts if c["is_right"]]
                left_items = [c for c in raw_callouts if not c["is_right"]]
                resolve_side(right_items)
                resolve_side(left_items)

                for item in right_items + left_items:
                    x1, y1 = item["x1"], item["y1"]
                    y2 = item["target_y"]
                    x2 = item["orig_x2"]
                    is_right = item["is_right"]
                    x3 = x2 + (0.16 if is_right else -0.16)
                    y3 = y2

                    ax.plot([x1, x2, x3], [y1, y2, y3], color=item["color"], linewidth=1.2, alpha=0.85)
                    text_x = x3 + (0.04 if is_right else -0.04)
                    ha = "left" if is_right else "right"
                    short_lbl = textwrap.shorten(item["label"], width=18, placeholder="..")
                    ax.text(text_x, y3, f"{short_lbl}  {item['pct']}", ha=ha, va="center",
                            fontsize=7.5, color="#1E293B", fontweight="bold")

                # Center total display for donut
                if is_donut:
                    metric_label = chart.datasets[0].label or "Total"
                    if "freight" in metric_label.lower() or "charge" in metric_label.lower() or "shipping" in metric_label.lower():
                        center_label = "Total Freight"
                    else:
                        center_label = metric_label[:14]

                    if total >= 1000:
                        total_formatted = f"₹{total:,.0f}"
                    else:
                        total_formatted = f"{total:,.0f}" if float(total).is_integer() else f"{total:,.1f}"

                    ax.text(0, 0.08, center_label, ha="center", va="center", fontsize=8, color="#64748B")
                    ax.text(0, -0.08, total_formatted, ha="center", va="center", fontsize=12, color="#1E293B", fontweight="bold")

                # Structured bottom legend
                legend_entries = []
                for i in range(len(values)):
                    v = values[i]
                    v_str = f"₹{v:,.0f}" if v >= 1000 else f"{v:,.0f}" if float(v).is_integer() else f"{v:,.1f}"
                    name = textwrap.shorten(labels[i], width=24, placeholder="..")
                    legend_entries.append((colors[i], f"{name} ({v_str})"))

                handles = [
                    plt.Line2D([0], [0], marker="s", color="w", markerfacecolor=c, markersize=6, label=txt)
                    for c, txt in legend_entries
                ]
                figure.legend(
                    handles=handles, loc="lower center", ncol=min(4, len(legend_entries)),
                    fontsize=6.8, frameon=False, bbox_to_anchor=(0.5, 0.01)
                )
            else:
                ax = figure.add_subplot(111)
                ax.text(0.5, 0.5, "No positive values to plot", ha="center", va="center", color="#64748B")
                ax.set_axis_off()
        else:
            figure = Figure(figsize=(7.2, 3.2), dpi=180)
            FigureCanvasAgg(figure)
            # Bar or line chart
            ax = figure.add_subplot(111)
            figure.subplots_adjust(bottom=0.22, top=0.88, left=0.10, right=0.96)
            for i, dataset in enumerate(chart.datasets):
                positions = list(range(len(labels)))
                color = _COLORS[i % len(_COLORS)]
                if chart_type in {"line", "area"}:
                    if chart_type == "area":
                        ax.fill_between(positions, dataset.data, color=color, alpha=0.42, label=dataset.label)
                        ax.plot(positions, dataset.data, marker="o", color=color, linewidth=2.0, markersize=4.5)
                    else:
                        ax.plot(positions, dataset.data, marker="o", label=dataset.label, color=color, linewidth=2.2, markersize=5)
                    if chart_type == "area":
                        pass
                    for pos, val in zip(positions, dataset.data):
                        if val > 0:
                            val_label = f"{val:,.0f}" if val >= 1000 else f"{val:,.1f}"
                            ax.annotate(
                                val_label,
                                xy=(pos, val),
                                xytext=(0, 4),
                                textcoords="offset points",
                                ha="center", va="bottom",
                                fontsize=6.5, color="#475569"
                            )
                else:
                    width = 0.8 / len(chart.datasets)
                    offset = [x - 0.4 + width * (i + 0.5) for x in positions]
                    bars = ax.bar(offset, dataset.data, width=width, label=dataset.label, color=color, edgecolor="none")
                    # Value labels above bars
                    for b in bars:
                        h = b.get_height()
                        if h > 0:
                            val_label = f"{h:,.0f}" if h >= 1000 else f"{h:,.1f}"
                            ax.annotate(
                                val_label,
                                xy=(b.get_x() + b.get_width() / 2, h),
                                xytext=(0, 2),
                                textcoords="offset points",
                                ha="center", va="bottom",
                                fontsize=6.5, color="#475569"
                            )

            short_labels = [textwrap.shorten(lbl, width=16, placeholder="..") for lbl in labels]
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels(short_labels, fontsize=7.5, rotation=15, ha="right")
            ax.set_ylim(bottom=0)
            ax.tick_params(axis="y", labelsize=7.5)
            ax.spines[["top", "right"]].set_visible(False)
            ax.spines[["left", "bottom"]].set_color("#CBD5E1")
            ax.grid(axis="y", color="#E2E8F0", linestyle="--", linewidth=0.6, alpha=0.8)
            ax.set_axisbelow(True)
            if len(chart.datasets) > 1 or (chart.datasets and chart.datasets[0].label):
                ax.legend(fontsize=7.5, loc="upper left", bbox_to_anchor=(0, 1.15), frameon=False, ncol=min(4, len(chart.datasets)))

        result = BytesIO()
        figure.savefig(result, format="png")
        return result.getvalue()
