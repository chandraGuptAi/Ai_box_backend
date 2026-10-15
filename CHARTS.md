# Chart contract (`structured_report.charts`)

Charts are calculated in Python from the request data. DeepSeek never supplies chart numbers. At most it picks
fields from an approved list when a chart request is ambiguous, and it does that inside the report's single call.

## When the chart engine is used

| Prompt | Charts |
|---|---|
| Names a chart type ("bar graph", "donut", "heat map", "box plot"…) or asks for a trend | Chart engine: every requested chart, in prompt order, up to 8 |
| Mentions charts/graphs without a type, or none at all | Unchanged: the previous automatic charts |
| Chart-only ("Show courier-wise cancellations using a bar graph.") | Chart engine, **no DeepSeek call**, one-line summary, no tables (`generation.ai_calls = 0`) |
| "remove/no/without the pie chart" | That type is not generated |

Discovery endpoint: `GET /api/skills/charts/registry` lists every chart skill (type, aliases, requirements,
max points, whether it exports as an image), plus each report type's allowed dimensions, numeric fields and measures.

## Chart object

All fields after `datasets` are new and optional. Legacy charts omit them (`null`/`{}`) and render as before.

```jsonc
{
  "id": "courier_orders",            // stable; legacy ids are kept for the same chart
  "type": "bar",                     // see the table below
  "title": "Orders by Courier",
  "labels": ["Delhivery", "Xpressbees"],
  "datasets": [{ "label": "Orders", "data": [5, 3] }],
  "x_axis": { "field": "courier", "label": "Courier", "unit": null, "format": "category" },
  "y_axis": { "field": "orders", "label": "Orders", "unit": null, "format": "number" },
  "value_format": { "type": "number", "decimals": 0, "currency": null, "unit": null },
  "options": { "showLegend": true, "showTooltip": true },
  "description": "Last 30 days of the supplied data, up to 2025-01-07.",   // short, optional
  "source": "rule"                   // rule | ai_plan | default; null on legacy charts
}
```

- Numbers are always numeric. Format them with `value_format`: `percent` gives `12.5%`, `currency` gives the amount
  with `decimals` (`currency: null` means the currency is as supplied), and `unit` (e.g. `kg`) is a suffix.
- `axis.format` is one of `category | date | number | currency | percent`.
- A chart that cannot be produced does not fail the report. It appears in `structured_report.chart_notes`:
  `{"requested_type": "Gauge chart", "title": null, "reason": "A gauge shows a single percentage; ...", "missing": ["percentage measure"]}`.
  The same reason is also added to `structured_report.limitations`, unless the user asked to hide limitations.

## Types and data shapes (Apache ECharts mapping)

| `type` | Data | ECharts |
|---|---|---|
| `bar` | `labels` + one dataset | `bar`; `options.horizontal` swaps axes |
| `horizontal_bar` | same | `bar` with `yAxis.type = "category"` |
| `grouped_bar` | `labels` = x; one dataset per series (`options.series_field`) | several `bar` series |
| `stacked_bar` | same | `bar` series with `stack: "total"` |
| `line` / `area` | `labels` = periods (`options.granularity`: day/week/month), one dataset per series | `line`; `area` adds `areaStyle` |
| `pie` / `doughnut` | `labels` + one dataset (small groups are combined into "Other") | `pie`; doughnut `radius: ["55%","75%"]` |
| `scatter` | `datasets[0].points[] = {x, y}` (no `data`) | `scatter` with `[x, y]` |
| `bubble` | `labels` = groups; `points[] = {x, y, r, label}`, where `r` is the record count | `scatter` with `symbolSize` from `r` |
| `histogram` | `labels` = bin ranges, data = counts | `bar` with `barCategoryGap: 0` |
| `heatmap` | `labels` = x; each dataset is a y row (`dataset.label`); `options.min/max` | `heatmap` + `visualMap` |
| `boxplot` | `labels` = groups; `datasets[0].boxes[] = {min,q1,median,q3,max,count}`; `data` = medians | `boxplot` with `[min,q1,median,q3,max]` |
| `treemap` | `labels` + one dataset | `treemap` with `{name, value}` |
| `funnel` | `labels` = stages in order; `options.sort = "none"` | `funnel` with `sort: "none"` |
| `radar` | `labels` = indicators (percent, `options.min/max` 0–100); dataset per group | `radar` |
| `waterfall` | `labels` + data; `options.steps[i]` is `total` or `decrease` | stacked `bar` with a transparent offset series |
| `gauge` | one label, one value; `options.min/max` | `gauge` |
| `map` | `labels` = canonical Indian state/UT names; `options.map = "india"` | `map` series (register an India GeoJSON whose feature names match) |

## Example: chart request that cannot be produced

`POST /api/reports/generate` with the prompt `"Show orders on an india map, a funnel chart, a gauge of orders"`
returns `charts` = map + funnel, and:

```json
"chart_notes": [{"requested_type": "Gauge chart", "title": null,
  "reason": "A gauge shows a single percentage; ask for a rate such as delivery rate or cancellation rate.",
  "missing": ["percentage measure"]}]
```

## Exports (PDF / Word)

Exports are rendered on the server with matplotlib, so no browser is needed. Every type except `map` is drawn as an
image. A map, or any chart that fails to render, is replaced by a table of its exact data with the note
"This chart type cannot be drawn in a document, so its data is shown as a table."

## Adding chart types or report types

- New chart type: `app.charts.skills.register(ChartSkill(type, label, aliases, requires, data_shape, max_points, build))`,
  and add the type to `ChartType` in `report_schema.py`.
- New report type: `app.charts.fields.register_adapter(report_type, DatasetAdapter(...))` with its fields, measures and
  a `rows(data)` function that returns flat, PII-free rows.
