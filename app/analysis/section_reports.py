"""Execute AnalysisPlan.sections and verify their public coverage against request-local evidence."""
from __future__ import annotations

import json
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from pydantic import BaseModel, ConfigDict, Field

from app.analysis.plan import ExecutionSection
from app.models.report_schema import ReportTable, ReportChart, ChartDataset, ChartNote

@dataclass
class SectionDataset:
    requirement: ExecutionSection
    text: str
    count: int
    tables: list[ReportTable] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    charts: list[ReportChart] = field(default_factory=list)
    chart_notes: list[ChartNote] = field(default_factory=list)
    unavailable: bool = False


def _duration(row):
    start, end, pickup = row.get("order_date"), row.get("delivery_date"), row.get("pickup_date")
    if start and end and end >= start and (not pickup or start <= pickup <= end):
        return (end - start).days
    return None


class GroundedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=1000)
    metric_ids: list[str] = Field(min_length=1, max_length=12)


class GroundedSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    claims: list[GroundedClaim] = Field(default_factory=list, max_length=3)


class GroundedNarrative(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sections: list[GroundedSection] = Field(max_length=20)


def narrative_messages(prompt, plan, datasets, scope):
    payload = {"original_request": prompt, "approved_ordered_plan": [s.model_dump() for s in plan.sections],
               "scope": scope, "datasets": [{"id": d.requirement.id, "metrics": d.metrics,
                   "tables": [t.model_dump() for t in d.tables], "limitations": d.limitations, "factual_text": d.text} for d in datasets],
               "schema": GroundedNarrative.model_json_schema()}
    return [{"role": "system", "content": "[grounded_sections] Return JSON narrative only for the approved ordered sections. "
        "Use supplied facts only; every claim references metric_ids. Do not invent reasons, identities, history or causal explanations. "
        "Buyer means consignee. Volume ranking is not best performance. Do not add tracking/data-debug issues. "
        "Do not add sections, KPIs, tables or values. Keep zero-result/unavailable sections and truthful limitations. "
        "When the plan requests recommendations, give conditional review actions grounded in metric references; "
        "do not assert unobserved causes or guarantee improvements."},
        {"role": "user", "content": json.dumps(payload, default=str, ensure_ascii=True)}]


def grounded_text(raw, datasets):
    """A bad claim repairs only its section, never replaces the approved report with a generic one."""
    narrative = GroundedNarrative.model_validate(raw)
    by_id = {s.id: s for s in narrative.sections}
    out, repaired = {}, False
    for dataset in datasets:
        identifier = dataset.requirement.id
        section = by_id.get(identifier)
        texts = []
        for claim in section.claims if section else []:
            valid = all(key in dataset.metrics and dataset.metrics[key] is not None for key in claim.metric_ids)
            allowed = {Decimal(str(dataset.metrics[key])) for key in claim.metric_ids if key in dataset.metrics and dataset.metrics[key] is not None}
            values = re.findall(r"(?<![\w])[-+]?\d[\d,]*(?:\.\d+)?", claim.text)
            valid = valid and all(Decimal(v.replace(",", "")) in allowed for v in values)
            valid = valid and not re.search(r"\b(?:caus\w*|because|due to|driven by|first.time|best performance|issues?|anomal\w*|tracking errors?)\b", claim.text, re.I)
            if valid:
                texts.append(claim.text)
        out[identifier] = texts or [dataset.text]
        repaired = repaired or not texts
    return out, repaired


def verify_section_coverage(report, plan, snapshot, datasets):
    """Independent raw-row recount, also checking the final serialized order and nested tables."""
    from app.analysis.accuracy import Audit, reference_row, subset, digest
    audit = Audit(snapshot, report.report_id)
    selected_timezone = json.loads(snapshot.filters_json).get("timezone", "Asia/Kolkata")
    references = [reference_row(r, selected_timezone) for r in snapshot.records()]
    for row in references:
        row["delivery_days"] = _duration(row) if row.get("status_category") == "delivered" else None
    rows = subset(references, plan)
    public = report.model_dump(mode="json")
    sections = (public.get("structured_report") or {}).get("sections") or []
    ids = [s["id"] for s in sections]
    expected_ids = [s.id for s in plan.sections]
    returned_plan = (public.get("structured_report") or {}).get("plan") or {}
    audit.add("approved_plan_preserved", returned_plan.get("sections") == [s.model_dump(mode="json") for s in plan.sections],
        "approved current execution plan", "preserved" if returned_plan.get("sections") == [s.model_dump(mode="json") for s in plan.sections] else "changed", "structured.plan")
    audit.add("section_order", ids == expected_ids, expected_ids, ids, "structured.sections")
    expected_table_ids = [table.id for dataset in datasets for table in dataset.tables]
    actual_table_ids = [table.get("id") for table in public.get("tables", [])]
    audit.add("export_table_order", actual_table_ids == expected_table_ids, expected_table_ids, actual_table_ids, "tables")
    content_titles = [s["heading"] for s in public["content"].get("sections", [])]
    expected_titles = [s.title for s in plan.sections]
    audit.add("content_section_order", content_titles == expected_titles, expected_titles, content_titles, "content.sections")
    expected_charts = [c.id for d in datasets for c in d.charts]
    actual_charts = [c["id"] for c in public["structured_report"].get("charts", [])]
    audit.add("chart_order", actual_charts == expected_charts, expected_charts, actual_charts, "structured.charts")
    for spec, dataset in zip(plan.sections, datasets):
        current = next((s for s in sections if s["id"] == spec.id), None)
        audit.add("coverage_"+spec.id, current is not None, True, current is not None, spec.id)
        if not current:
            continue
        verify_registered_section(audit, spec, current, public, rows, dataset)
    audit.finish()  # Essential requested coverage is enforced even when generic accuracy checks are audit-only.


def section_title(spec):
    return spec.title


def _registered_rows(spec, rows):
    from app.analysis.executor import matching_rows
    from app.analysis.plan import AnalysisPlan, Filter
    return matching_rows(rows, AnalysisPlan(filters=tuple(Filter(f.field, f.op, f.value) for f in spec.filters)))[0]


def _required_fields(spec, adapter):
    required = set(spec.required_fields) | set(spec.dimensions) | {f.field for f in spec.filters}
    for name in spec.metrics:
        metric = adapter.measures[name]
        if metric.field:
            required.add(metric.field)
        required.update(field for field, _ in metric.where)
    return required


def execute_registered_section(spec, rows):
    """Compose the existing adapter's registered operations; no section-name dispatch."""
    from app.analysis.executor import make_table
    from app.charts.fields import get_adapter
    from app.charts.aggregate import measure
    from app.errors import ClarificationRequiredError
    adapter = get_adapter("shipment")
    items = _registered_rows(spec, rows)
    result = SectionDataset(spec, f"{len(items)} shipments match this section's analysis subset.", len(items),
        metrics={spec.id+".shipments": len(items)})
    required = _required_fields(spec, adapter)
    missing = sorted(f for f in required if rows and not any(r.get(f) not in (None, "") for r in rows))
    if missing:
        result.limitations.append("Data not provided: " + ", ".join(adapter.fields[f].label for f in missing) + ".")
        if spec.missing_data == "clarify":
            raise ClarificationRequiredError("Required fields are unavailable for the requested analysis.",
                details={"clarification_required": True, "section_id": spec.id, "missing_fields": missing})
    if "buyer" in spec.dimensions:
        result.limitations.append("Buyer means consignee; opaque labels identify grouping keys.")
        if any(r.get("buyer_identity_basis", "").startswith("approximate") for r in items):
            result.limitations.append("Consignee identities are approximate name/city/pincode groupings, not verified people or first-time buyers.")
    result.limitations.append(f"Section denominator: {len(items)} shipments after this section's filters. Rates use this subset within each group; averages exclude missing metric values.")
    for condition in spec.filters:
        result.limitations.append(f"Analysis filter: {adapter.fields[condition.field].label} {condition.op} {condition.value}.")
    if spec.ranking_metric:
        result.limitations.append(f"Ranking basis: {adapter.measures[spec.ranking_metric].label}, {spec.direction}.")
    if missing:
        result.unavailable = True
        result.text = "No data available for " + spec.title + "."
        result.metrics[spec.id+".shipments"] = None
    elif not items:
        result.text = "No matching shipments for " + spec.title + " in the selected data."
    def metric_value(group, name):
        if adapter.measures[name].agg == "share":
            return round(100*len(group)/len(items), 1) if items else None
        return measure(group, adapter.measures[name])
    for name in spec.metrics:
        result.metrics[f"{spec.id}.total.{name}"] = metric_value(items, name) if not missing else None
    if not result.unavailable:
        groups = defaultdict(list)
        for row in items:
            key = tuple(str(row[d]) if row.get(d) not in (None, "") else "Not available" for d in spec.dimensions)
            groups[key].append(row)
        if not spec.dimensions:
            groups[()] = items
        records = []
        for key, group in groups.items():
            values = [metric_value(group, m) for m in spec.metrics]
            records.append([*key, *values])
        if spec.ranking_metric:
            col = len(spec.dimensions) + spec.metrics.index(spec.ranking_metric)
            records.sort(key=lambda r: (r[col] is None, -(r[col] or 0) if spec.direction == "desc" else (r[col] or 0), tuple(r[:len(spec.dimensions)])))
        else:
            records.sort(key=lambda r: tuple(r[:len(spec.dimensions)]))
        columns = [(d, adapter.fields[d].label, "text", None) for d in spec.dimensions]
        columns += [(m, adapter.measures[m].label,
            "currency" if adapter.measures[m].value_format == "currency" else "percent" if adapter.measures[m].agg in {"rate", "share"} else "number",
            0 if adapter.measures[m].agg == "count" else 2) for m in spec.metrics]
        result.tables = [make_table(spec.title, columns, records[:spec.limit], total_rows=len(records),
            table_id="section_"+spec.id, note=f"Denominator: {len(items)} shipments; ranking: {spec.ranking_metric or 'dimensions'}, {spec.direction}. Showing up to {spec.limit} groups; omitted groups are not zero.")]
        for index, row in enumerate(records[:spec.limit]):
            for name, value in zip(spec.metrics, row[len(spec.dimensions):]):
                result.metrics[f"{spec.id}.group_{index}.{name}"] = value
        partial = sum(any(r.get(d) in (None, "") for d in spec.dimensions) for r in items)
        if partial:
            result.limitations.append(f"{partial} shipments have partial grouping values, shown as Not available.")
    for index, kind in enumerate(spec.charts):
        metric = spec.ranking_metric or spec.metrics[0]
        table = result.tables[0] if result.tables else None
        col = len(spec.dimensions) + spec.metrics.index(metric)
        numeric_fields = tuple(dict.fromkeys(adapter.measures[m].field for m in spec.metrics if adapter.measures[m].field))
        if kind in {"histogram", "scatter"} and not result.unavailable and len(numeric_fields) >= (2 if kind == "scatter" else 1):
            from app.charts.skills import SKILLS, ChartRequest, BuildContext, ChartUnavailable
            try:
                chart = SKILLS[kind].build(ChartRequest(type=kind, numeric=numeric_fields, title=spec.title), BuildContext(adapter, items))
                chart.id = f"section_{spec.id}_{index}"
                result.charts.append(chart)
            except ChartUnavailable as exc:
                result.chart_notes.append(ChartNote(title=spec.title, requested_type=kind, reason=exc.reason))
        elif table and spec.dimensions and kind in {"bar", "line", "area", "pie", "doughnut"} and all(r[col] is not None for r in table.values) and table.values and (kind not in {"pie", "doughnut"} or adapter.measures[metric].additive):
            result.charts.append(ReportChart(id=f"section_{spec.id}_{index}", title=spec.title, type=kind,
                labels=[" / ".join(str(v) for v in r[:len(spec.dimensions)]) for r in table.values],
                datasets=[ChartDataset(label=adapter.measures[metric].label, data=[float(r[col]) for r in table.values])]))
        else:
            result.chart_notes.append(ChartNote(title=spec.title, requested_type=kind,
                reason="No data available for this visual using the requested fields and metric."))
    return result


def verify_registered_section(audit, spec, current, public, rows, dataset):
    """Independently aggregate raw-normalized evidence, then inspect final public values and visuals."""
    from app.charts.fields import get_adapter
    from app.analysis.accuracy import digest
    adapter = get_adapter("shipment")
    # Independent filter implementation used by the accuracy module.
    from app.analysis.accuracy import _matches
    items = [r for r in rows if all(_matches(r, f) for f in spec.filters)]
    required = _required_fields(spec, adapter)
    missing = [f for f in required if rows and not any(r.get(f) not in (None, "") for r in rows)]
    paragraphs = [b.get("text", "") for b in current["blocks"] if b["type"] == "paragraph"]
    tables = [b for b in current["blocks"] if b["type"] == "table"]
    valid_result = dataset.count == len(items) and dataset.unavailable == bool(missing)
    if missing or not items:
        valid_result = valid_result and dataset.text in paragraphs
    audit.add("section_result_"+spec.id, valid_result, len(items), dataset.count, spec.id)
    audit.add("section_label_"+spec.id, current["title"] == spec.title, spec.title, current["title"], spec.id)
    def ref_metric(group, name):
        metric = adapter.measures[name]
        if metric.agg == "share":
            return round(100*len(group)/len(items), 1) if items else None
        if metric.agg in {"count", "rate"}:
            count = sum(all(r.get(f) in vals for f, vals in metric.where) for r in group)
            return float(count) if metric.agg == "count" else round(100*count/len(group), 1) if group else None
        numbers = sorted(Decimal(str(r[metric.field])) for r in group if r.get(metric.field) is not None)
        if not numbers:
            return None
        value = sum(numbers) if metric.agg == "sum" else statistics.median(numbers) if metric.agg == "median" else max(numbers) if metric.agg == "max" else sum(numbers)/len(numbers)
        return float(value.quantize(Decimal(".01"), rounding=ROUND_HALF_UP))
    groups = defaultdict(list)
    for row in items:
        key = tuple(str(row[d]) if row.get(d) not in (None, "") else "Not available" for d in spec.dimensions)
        groups[key].append(row)
    if not spec.dimensions:
        groups[()] = items
    expected = [[*key, *[ref_metric(group, m) for m in spec.metrics]] for key, group in groups.items()]
    dims = len(spec.dimensions)
    if spec.ranking_metric:
        col = dims + spec.metrics.index(spec.ranking_metric)
        expected.sort(key=lambda r: (r[col] is None, -(r[col] or 0) if spec.direction == "desc" else (r[col] or 0), tuple(r[:dims])))
    else:
        expected.sort(key=lambda r: tuple(r[:dims]))
    expected = [] if missing else expected[:spec.limit]
    actual = tables[0].get("values", []) if tables else []
    audit.add("section_values_"+spec.id, actual == expected, digest(expected), digest(actual), spec.id,
        "independent registered metrics/grouping/ranking; values withheld")
    expected_keys = [*spec.dimensions, *spec.metrics]
    if tables:
        audit.add("section_dimensions_"+spec.id, [c["key"] for c in tables[0]["column_meta"]] == expected_keys,
            expected_keys, [c["key"] for c in tables[0]["column_meta"]], spec.id)
        labels = [adapter.fields[d].label for d in spec.dimensions] + [adapter.measures[m].label for m in spec.metrics]
        actual_labels = tables[0].get("columns", [])
        audit.add("section_labels_"+spec.id, actual_labels == labels and [c["label"] for c in tables[0]["column_meta"]] == labels,
            labels, actual_labels, spec.id)
        top = next((t for t in public.get("tables", []) if t.get("id") == tables[0].get("id")), None)
        same_table = top == {key: value for key, value in tables[0].items() if key != "type"}
        audit.add("export_table_"+spec.id, same_table, True, same_table, spec.id)
    public_metrics = public.get("metrics", {}).get("section_metrics", {})
    expected_metrics = {spec.id+".shipments": None if missing else len(items)}
    for index, row in enumerate(expected):
        for name, value in zip(spec.metrics, row[dims:]):
            expected_metrics[f"{spec.id}.group_{index}.{name}"] = value
    for name in spec.metrics:
        key = f"{spec.id}.total.{name}"
        expected_total = None if missing else ref_metric(items, name)
        expected_metrics[key] = expected_total
        actual_total = public_metrics.get(key)
        audit.add("section_total_"+spec.id+"_"+name, actual_total == expected_total, expected_total, actual_total, spec.id)
    actual_metrics = {k: v for k, v in public_metrics.items() if k.startswith(spec.id+".")}
    audit.add("metric_references_"+spec.id, actual_metrics == expected_metrics, digest(expected_metrics), digest(actual_metrics), spec.id)
    for card in public["structured_report"].get("metrics", []):
        for name in spec.metrics:
            if card["key"] == (f"{spec.id}.total.{name}").replace(".", "_"):
                value = expected_metrics[f"{spec.id}.total.{name}"]
                valid = value is not None and card["value"] == value and card["label"] == spec.title + ": " + adapter.measures[name].label
                audit.add("metric_card_"+spec.id, valid, value, card["value"], spec.id)
    charts = [c for c in public["structured_report"].get("charts", []) if c["id"].startswith("section_"+spec.id+"_")]
    notes = public["structured_report"].get("chart_notes", [])
    for kind in spec.charts:
        fulfilled = any(c["type"] == kind for c in charts) or any(n["requested_type"] == kind and n.get("title") == spec.title for n in notes)
        audit.add("chart_coverage_"+spec.id, fulfilled, kind, kind if fulfilled else "missing", spec.id)
    for chart in charts:
        if chart["type"] in {"histogram", "scatter"}:
            from app.charts.skills import SKILLS
            import math
            fields = list(dict.fromkeys(adapter.measures[m].field for m in spec.metrics if adapter.measures[m].field))
            if chart["type"] == "scatter":
                expected_points = [(r[fields[0]], r[fields[1]]) for r in items if r.get(fields[0]) is not None and r.get(fields[1]) is not None][:SKILLS["scatter"].max_points]
                actual_points = [(p["x"], p["y"]) for p in chart["datasets"][0]["points"]]
                valid = actual_points == expected_points and chart["x_axis"]["field"] == fields[0] and chart["y_axis"]["field"] == fields[1]
            else:
                nums = [float(r[fields[0]]) for r in items if r.get(fields[0]) is not None]
                low, high = min(nums), max(nums)
                size = 1 if low == high else max(2, min(SKILLS["histogram"].max_points, math.ceil(math.log2(len(nums)))+1))
                counts = [0]*size
                for value in nums:
                    index = 0 if low == high else min(size-1, int((value-low)/((high-low)/size)))
                    counts[index] += 1
                def fmt(v):
                    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.2f}".rstrip("0").rstrip(".")
                labels = [fmt(low)] if low == high else [fmt(low+i*(high-low)/size)+"\u2013"+fmt(low+(i+1)*(high-low)/size) for i in range(size)]
                valid = chart["datasets"][0]["data"] == counts and chart["labels"] == labels and chart["x_axis"]["field"] == fields[0]
            audit.add("chart_values_"+spec.id, valid, "independent numeric distribution/points", "verified" if valid else "mismatch", spec.id)
            continue
        metric = spec.ranking_metric or spec.metrics[0]
        col = dims + spec.metrics.index(metric)
        values = [r[col] for r in expected]
        labels = [" / ".join(str(v) for v in row[:dims]) for row in expected]
        valid = chart["labels"] == labels and chart["datasets"][0]["data"] == values and chart["datasets"][0]["label"] == adapter.measures[metric].label
        audit.add("chart_values_"+spec.id, valid, digest(values), digest(chart["datasets"][0]["data"]), spec.id)
    for requirement in spec.requirement_ids:
        audit.add("requirement_"+requirement, valid_result and actual == expected, "fulfilled or explicitly unavailable",
            "fulfilled" if valid_result and actual == expected else "failed", spec.id)
