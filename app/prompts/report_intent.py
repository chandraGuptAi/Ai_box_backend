"""Small, deterministic shipment report plans; no classifier or extra AI call."""
from dataclasses import dataclass
import re

from app.models.report_schema import ChartDataset, DisplayMetric, ReportChart, ReportTable
from app.prompts.base import AnalysisResult
from app.prompts.shipment_financial import financial_focus, shipping_financial_metrics, financial_tables


STATUS_LABELS = {"delivered": "Delivered", "cancelled": "Cancelled", "rto": "RTO", "in_transit": "In-Transit",
                 "pending": "Pending", "exception": "Exception", "lost": "Lost", "other": "Other", "unknown": "Status-Missing"}


@dataclass(frozen=True)
class ReportIntent:
    mode: str = "detailed"
    topics: tuple[str, ...] = ("status",)
    status: str | None = None
    charts: bool = False
    tables: bool = True
    recommendations: bool = True
    donut: bool = False
    omit_courier_charges_table: bool = False
    chart_courier_charges: bool = False
    single_chart: bool = False
    pie_courier: bool = False
    all_tables: bool = False
    omit_limitations: bool = False
    both_circular: bool = False
    wants_bar: bool = False
    wants_area: bool = False
    chart_specs: tuple[dict, ...] = ()
    excluded_charts: tuple[str, ...] = ()
    focused: bool = False   # set from the analysis plan: only explicitly relevant tables and charts

    @property
    def status_label(self):
        return STATUS_LABELS.get(self.status, str(self.status).replace("_", " ").title()) if self.status else None

    @property
    def compact(self):
        return self.mode in {"dashboard", "summary", "table_only", "graphs_only"}

    @property
    def deterministic(self):
        return self.mode in {"table_only", "graphs_only"}


def extract_chart_specs(text: str) -> list[dict]:
    text_lower = text.lower()
    items = re.findall(
        r"(?:^|\n|\b)(\d+)[\.\)]\s*(?:an?\s+)?([a-z/]+)\s+(?:chart|graph)\b([^\n\.]*)",
        text_lower
    )
    specs = []
    for num, raw_type, desc in items:
        ctype = ('area' if 'area' in raw_type
                 else 'line' if 'line' in raw_type
                 else 'bar' if any(w in raw_type for w in ['bar', 'column'])
                 else 'doughnut' if any(w in raw_type for w in ['donut', 'doughnut'])
                 else 'pie' if 'pie' in raw_type
                 else 'bar')
        topic = ('courier' if any(w in desc for w in ['courier', 'carrier'])
                 else 'status' if any(w in desc for w in ['status', 'delivery', 'rto', 'transit', 'undelivered', 'exception'])
                 else 'financial' if any(w in desc for w in ['charge', 'freight', 'cost', 'payment'])
                 else 'geography' if any(w in desc for w in ['city', 'state', 'destination', 'geo'])
                 else None)
        specs.append({"num": int(num), "type": ctype, "topic": topic, "desc": desc.strip()})
    return specs


def detect_intent(prompt):
    text = prompt.lower()

    types = [
        ("area", r"\b(?:remove|delete|omit|drop|hide|exclude|no|without)\s+(?:the\s+|this\s+|an?\s+)?area\b|\barea\s+(?:charts?|graphs?)\s*(?:ko\s+)?(?:hata|mat|remove|delete|drop)\b"),
        ("line", r"\b(?:remove|delete|omit|drop|hide|exclude|no|without)\s+(?:the\s+|this\s+|an?\s+)?line\b|\bline\s+(?:charts?|graphs?)\s*(?:ko\s+)?(?:hata|mat|remove|delete|drop)\b"),
        ("bar", r"\b(?:remove|delete|omit|drop|hide|exclude|no|without)\s+(?:the\s+|this\s+|an?\s+)?(?:bar|column)\b|\b(?:bar|column)\s+(?:charts?|graphs?)\s*(?:ko\s+)?(?:hata|mat|remove|delete|drop)\b"),
        ("pie", r"\b(?:remove|delete|omit|drop|hide|exclude|no|without)\s+(?:the\s+|this\s+|an?\s+)?pie\b|\bpie\s+(?:charts?|graphs?)\s*(?:ko\s+)?(?:hata|mat|remove|delete|drop)\b"),
        ("donut", r"\b(?:remove|delete|omit|drop|hide|exclude|no|without)\s+(?:the\s+|this\s+|an?\s+)?(?:donut|doughnut)\b|\b(?:donut|doughnut)\s+(?:charts?|graphs?)\s*(?:ko\s+)?(?:hata|mat|remove|delete|drop)\b"),
        ("first", r"\b(?:remove|delete|omit|drop|hide|exclude|no|without)\s+(?:the\s+|this\s+|an?\s+)?(?:first|1st|pehla)\s*(?:charts?|graphs?)?\b|\b(?:first|1st|pehla)\s*(?:charts?|graphs?)\s*(?:ko\s+)?(?:hata|mat|remove|delete|drop)\b"),
        ("second", r"\b(?:remove|delete|omit|drop|hide|exclude|no|without)\s+(?:the\s+|this\s+|an?\s+)?(?:second|2nd|dusra)\s*(?:charts?|graphs?)?\b|\b(?:second|2nd|dusra)\s*(?:charts?|graphs?)\s*(?:ko\s+)?(?:hata|mat|remove|delete|drop)\b"),
        ("courier", r"\b(?:remove|delete|omit|drop|hide|exclude|no|without)\s+(?:the\s+|this\s+|an?\s+)?courier\s+(?:charts?|graphs?)\b|\bcourier\s+(?:charts?|graphs?)\s*(?:ko\s+)?(?:hata|mat|remove|delete|drop)\b"),
        ("status", r"\b(?:remove|delete|omit|drop|hide|exclude|no|without)\s+(?:the\s+|this\s+|an?\s+)?(?:status|delivery)\s+(?:charts?|graphs?)\b|\b(?:status|delivery)\s+(?:charts?|graphs?)\s*(?:ko\s+)?(?:hata|mat|remove|delete|drop)\b"),
    ]

    excluded_charts_list: list[str] = []
    for name, pat in types:
        if re.search(pat, text):
            if name == "pie" and re.search(r"\b(?:show|add|in)\s+.*?\bpie\s+chart\b", text):
                continue
            if name == "donut" and re.search(r"\b(?:show|add|in)\s+.*?\b(?:donut|doughnut)\s+chart\b", text):
                continue
            if name == "donut":
                excluded_charts_list.extend(["donut", "doughnut"])
            else:
                excluded_charts_list.append(name)

    remove_all_charts = (
        bool(re.search(
            r"\b(?:no|without|remove|delete|omit|drop|hide|exclude)\s+(?:all\s+)?(?:the\s+|this\s+|that\s+)?(?:graphs?|charts?|visualizations?|visuals?)\b"
            r"|\b(?:graphs?|charts?|visuals?)\s*(?:ko\s+)?(?:hata\s*(?:d[eo]|o|ye)?|mat\s+dikh?a[on]?|nahi\s+chahiye|remove|delete|drop)\b(?!\s+(?:the\s+|this\s+|that\s+|a\s+|an\s+)?tables?\b)"
            r"|\b(?:hata\s*(?:d[eo]|o|ye)?|mat\s+dikh?a[on]?)\s+(?:all\s+)?(?:the\s+|this\s+|that\s+)?(?:graphs?|charts?)\b",
            text
        ))
        and not bool(excluded_charts_list)
    )

    if remove_all_charts or (bool(re.search(r"\b(no|without)\s+(?:all\s+)?(?:graphs?|charts?|visuals?)\b", text)) and not bool(excluded_charts_list)):
        excluded_charts_list = ["all"]

    has_positive_graph_word = bool(re.search(r"\b(graphs?|charts?|visualizations?|donut|doughnut)\b", text)) and not (
        remove_all_charts or bool(re.search(r"\b(no|without)\s+(graphs?|charts?)", text))
    )
    graphs = has_positive_graph_word
    tables = bool(re.search(r"\b(tables?|tabular)\b", text)) and not re.search(r"\b(no|without)\s+tables?", text)
    only = bool(re.search(r"\b(only|just)\b", text))
    all_tables = bool(re.search(
        r"\b(?:all\s+(?:[\w]+\s+){0,4}tables?|multiple\s+tables?|show\s+all\s+tables?|every\s+table|full\s+tables?|sa?are?\s+(?:[\w]+\s+){0,4}tables?|sab\s+(?:[\w]+\s+){0,4}tables?|har\s+table)\b",
        text
    ))
    full_report = bool(re.search(r"\b(detailed|comprehensive|in.depth|full\s+report)\b", text))
    mode = ("table_only" if only and tables and not graphs else
            "graphs_only" if only and graphs and not tables else
            "dashboard" if "dashboard" in text else
            "detailed" if full_report else
            "summary" if re.search(r"\b(short|brief|concise)\b", text) else
            "comparison" if re.search(r"compar", text) else "detailed")
    financial = financial_focus(text)
    topics = []
    if financial:
        topics.append("financial")
    for name, pattern in (("courier", r"courier|carrier"), ("payment", r"payment|\bcod\b|prepaid|topay"),
                          ("cancellation", r"cancel"), ("geography", r"geograph|destination|\bstates?\b|\bcities\b"),
                          ("exception", r"exception|ndr|fail|undeliver|loss|lost|damage"),
                          ("status", r"status|deliver|\brto\b|performance|overall")):
        if re.search(pattern, text):
            topics.append(name)
    if financial and not all_tables:
        topics = [t for t in topics if t not in {"status", "cancellation"}]
        topics.extend(t for t in ("courier", "payment") if t not in topics)
    elif all_tables:
        for t in ("courier", "payment", "status", "cancellation", "geography"):
            if t not in topics:
                topics.append(t)
    if not topics:
        topics = ["status"]
    # Scope only unequivocal single-status requests, never comparisons of outcomes.
    status = None
    if re.search(r"\bdelivered(?:[- ]only)?\s+(?:shipments?|orders?|report)\b|\bdelivered.only\b", text) and not re.search(
            r"cancel|undelivered|\brto\b|pending|in.transit|all statuses|overall", text):
        status = "delivered"
        if "courier" not in topics:
            topics.append("courier")
    tables = (tables or all_tables or mode in {"detailed", "comparison"}) and not re.search(r"\b(no|without)\s+tables?", text)
    recommendations = mode in {"detailed", "comparison"} or bool(re.search(r"recommend|suggest|optimization", text))
    no_graphs = "all" in excluded_charts_list or bool(re.search(r"\b(no|without)\s+(graphs?|charts?|visuals?)\b", text))
    has_graph_word = bool(re.search(r"\b(graphs?|charts?|visualizations?|visuals?|donut|doughnut)\b", text)) and not (
        no_graphs or bool(re.search(r"\b(?:remove|delete|omit|drop|hide|exclude)\s+.*?\b(?:graphs?|charts?|visualizations?|visuals?)\b|\b(?:graphs?|charts?|visuals?)\s*(?:hata|mat\s+dikh|nahi|remove|delete|drop)\b(?!\s+(?:the\s+|this\s+|that\s+|a\s+|an\s+)?tables?\b)", text))
    )
    graphs = (not no_graphs) and (has_graph_word or (mode in {"detailed", "comparison"} and not bool(excluded_charts_list)))

    donut = (
        bool(re.search(r"\b(donut|doughnut)s?\b|\bthis\s+type\s+of\s+chart\b|\bvariable\s+radius\b", text))
        and not any(x in excluded_charts_list for x in ("all", "donut", "doughnut"))
    )
    omit_courier_charges_table = bool(re.search(
        r"\b(?:remove|omit|without|no)\b.*?\b(?:shipping charges by courier|courier charges)\b.*?\btables?\b|\btables?\b.*?\b(?:remove|omit|without|no)\b.*?\b(?:shipping charges by courier|courier charges)\b|\bremove\s+the\s+table\s+of\s+shipping\s+charges\b",
        text
    ))
    pie_courier = bool(re.search(
        r"\b(?:shipping charges|courier charges|courier freight)\b.*?\bpie\b|\bpie\b.*?\b(?:shipping charges|courier charges|courier freight)\b",
        text
    )) and not any(x in excluded_charts_list for x in ("all", "pie"))
    chart_courier_charges = bool(re.search(
        r"\b(?:shipping charges|courier charges|courier freight|freight distribution|freight charges|charges by courier)\b.*?\b(?:pie|donut|doughnut)\b|\b(?:pie|donut|doughnut)\b.*?\b(?:shipping charges|courier charges|courier freight|freight distribution|freight charges|charges by courier)\b",
        text
    )) and not any(x in excluded_charts_list for x in ("all", "pie", "donut", "doughnut", "courier"))
    single_chart = bool(re.search(
        r"\b(?:single\s+(?:graph|chart|visualization)|one\s+(?:graph|chart|visualization)|take\s+single\s+graph|only\s+one\s+(?:graph|chart)|single\s+area|single\s+donut)\b",
        text
    ))
    omit_limitations = bool(re.search(
        r"\b(?:hata\s*d[eo]|hatao|hataye|mat\s+dikh?ao|mat\s+dikh?ana|nahi\s+chahiye|na\s+dikh?ao|remove|omit|without|exclude|skip|delete|drop|hide|no)\b.*?\b(?:data\s+scop[er]?|limitations?|data\s+limitations?|scop[er]?)\b"
        r"|\b(?:data\s+scop[er]?|limitations?|data\s+limitations?|scop[er]?)\b.*?\b(?:hata\s*d[eo]|hatao|hataye|mat\s+dikh?ao|mat\s+dikh?ana|nahi\s+chahiye|na\s+dikh?ao|remove|omit|without|exclude|skip|delete|drop|hide)\b",
        text
    ))
    both_circular = bool(re.search(
        r"\b(?:dono\s+(?:graph|chart)[\w\s]*(?:pie|donut|doughnut|circle)|both\s+(?:graphs?|charts?)\s+(?:pie|donut|doughnut|circular)|dono\s+(?:me\s+)?(?:pie|donut))\b"
        r"|\b(?:donut|doughnut|pie)\b.*?\bdono\s+(?:graph|chart)s?\b",
        text
    ))
    wants_bar = (
        bool(re.search(r"\b(?:bar|column|baar)\s*(?:charts?|graphs?|cha?rt)\b|\b(?:bar|column)\s+chart\b", text))
        and not any(x in excluded_charts_list for x in ("all", "bar", "column"))
    )
    wants_area = (
        bool(re.search(r"\b(?:area[- ](?:charts?|graphs?|plots?)|area\s+(?:charts?|graphs?)|(?:in|as|with)\s+area(?:[- ](?:charts?|graphs?))?)\b", text))
        and not any(x in excluded_charts_list for x in ("all", "area"))
    )

    raw_chart_specs = extract_chart_specs(text)
    chart_specs = [
        s for s in raw_chart_specs
        if s.get("type") not in excluded_charts_list
        and s.get("topic") not in excluded_charts_list
    ] if "all" not in excluded_charts_list else []
    return ReportIntent(mode, tuple(topics), status, bool(graphs), bool(tables), recommendations, donut,
                        omit_courier_charges_table, chart_courier_charges, single_chart, pie_courier, all_tables,
                        omit_limitations, both_circular, wants_bar, wants_area, tuple(chart_specs), tuple(excluded_charts_list))


def scope_analysis(analysis, data, intent, keep=None):
    """Recalculate the analysis for a subset: the plan's record indexes (`keep`) or the intent's status."""
    if keep is None and not intent.status:
        return analysis
    from app.prompts.shipment_prompt import extract_orders, parse_order, compute_metrics, build_tables, build_key_metrics
    pairs = [(record, parse_order(record)) for record in extract_orders(data)]
    if keep is not None:
        pairs = [pair for index, pair in enumerate(pairs) if index in keep]
    else:
        pairs = [(record, order) for record, order in pairs if order.category == intent.status]
    metrics = compute_metrics([o for _, o in pairs])
    metrics["shipping_financial"] = shipping_financial_metrics([r for r, _ in pairs], [o for _, o in pairs])
    metrics["scope"] = {**analysis.metrics["scope"], "source_records": analysis.records_analyzed,
                        "records_analyzed": len(pairs), "status_filter": intent.status,
                        "denominator": "matching_supplied_records"}
    return AnalysisResult(len(pairs), metrics, build_key_metrics(metrics),
                          build_tables(metrics) + financial_tables(metrics["shipping_financial"]), {})


def selected_tables(analysis, intent):
    if not intent.tables or intent.mode == "graphs_only":
        return {}
    wanted = set()
    if intent.all_tables:
        wanted = {t.title for t in analysis.tables}
    else:
        financial_only = "financial" in intent.topics and not ("status" in intent.topics or "cancellation" in intent.topics or "exception" in intent.topics)
        if financial_only:
            wanted.update(("Shipping Charges by Courier", "Shipping Charges by Payment Mode"))
        else:
            # Focused requests get financial tables only when they ask about money.
            financial = "financial" in intent.topics or (not intent.focused and bool(
                analysis.metrics.get("shipping_financial", {}).get("total_shipping_charges")))
            if financial:
                wanted.update(("Shipping Charges by Courier", "Shipping Charges by Payment Mode"))
            for topic, title in (("status", "Shipment Status Summary"), ("courier", "Courier Performance"),
                                 ("payment", "Payment Mode"), ("cancellation", "Top Cancellation Reasons"),
                                 ("geography", "Top Destination States"), ("exception", "Shipment Status Summary")):
                if topic in intent.topics:
                    wanted.add(title)
            if (not intent.focused or "status" in intent.topics) and any(t.title == "Account-Level Status Benchmark" for t in analysis.tables):
                wanted.add("Account-Level Status Benchmark")
    if intent.omit_courier_charges_table:
        wanted.discard("Shipping Charges by Courier")
    result = {}
    for i, table in enumerate(analysis.tables):
        if table.title not in wanted or not table.rows:
            continue
        rows = table.rows[:8] if intent.compact else table.rows
        columns = table.columns
        if intent.status and table.title == "Courier Performance":
            # Every record in a single-status cohort has that status, so its order count is the status count.
            columns = ["Courier", f"{intent.status_label} Orders"]
            rows = [[row[0], row[1]] for row in rows]
        result[f"table_{i}"] = ReportTable(title=table.title, columns=columns, rows=rows)
    if intent.mode == "table_only" and len(result) > 1:
        result = dict(list(result.items())[:1])
    return result


def relevant_metrics(analysis, intent):
    metrics = analysis.metrics
    keys = {"total_orders", "order_date_range", "scope", "account_summary", "comparisons"}
    mapping = {"status": ("status_category_breakdown", "rates"),
               "courier": ("courier_performance", "distinct_couriers"),
               "payment": ("payment_mode_breakdown",),
               "cancellation": ("cancellation_reasons", "rates"),
               "exception": ("status_category_breakdown", "rates", "cancellation_reasons"),
               "geography": ("top_destination_states",),
               "financial": ("shipping_financial",)}
    for topic in intent.topics:
        keys.update(mapping.get(topic, ()))
    if "financial" in intent.topics and not intent.all_tables:
        keys.difference_update({"courier_performance", "payment_mode_breakdown", "distinct_couriers"})
    elif intent.all_tables:
        for val in mapping.values():
            keys.update(val)
    result = {k: metrics[k] for k in sorted(keys) if k in metrics}
    if intent.status and "courier_performance" in result:
        keys = ("courier", "total_orders") + ((intent.status,) if intent.status in ("delivered", "cancelled", "rto") else ())
        result["courier_performance"] = [{k: row[k] for k in keys} for row in result["courier_performance"]]
        result.pop("rates", None)  # A filtered cohort does not establish overall delivery performance.
        result.pop("status_category_breakdown", None)  # Its 100% is a filter artifact, not courier performance.
    # Bound the AI context; full selected tables are still rendered by Python.
    limit = 8 if intent.compact else 20
    for key, value in list(result.items()):
        if isinstance(value, list):
            result[key] = value[:limit]
    if "shipping_financial" in result:
        result["shipping_financial"] = {k: (v[:limit] if isinstance(v, list) else v)
                                         for k, v in result["shipping_financial"].items()
                                         if k not in {"cod_shipping_charges", "prepaid_shipping_charges", "topay_shipping_charges"}}
    return result


def relevant_limitations(analysis, intent):
    if intent.omit_limitations:
        return []
    notes = []
    scope = analysis.metrics.get("scope", {})
    if scope.get("kind") in {"sample", "paginated"}:
        notes.append("Results cover only the supplied records; full dataset coverage is not established.")
    if intent.status:
        notes.append(f"Only {intent.status_label.lower()} records are included; this subset does not establish overall "
                     "delivery performance.")
    coverage = analysis.metrics.get("field_coverage", {})
    for topic, field in (("courier", "courier"), ("payment", "payment_mode"), ("geography", "destination_state")):
        if topic in intent.topics and coverage.get(field, {}).get("present", analysis.records_analyzed) < analysis.records_analyzed:
            notes.append(f"Some records have no {field.replace('_', ' ')}; missing labels are shown separately.")
    if "financial" in intent.topics:
        notes.append("Shipping charges are not collected revenue, profit or confirmed losses. Currency is as supplied.")
        f = analysis.metrics["shipping_financial"]
        if f["orders_with_missing_charge"] or f["orders_with_invalid_charge"]:
            notes.append("Missing or invalid charges are excluded; averages use only orders with valid charges.")
    if "cancellation" in intent.topics:
        c = analysis.metrics["cancellation_reasons"]
        if c["cancelled_orders_with_reason"] < c["cancelled_orders"]:
            notes.append("Some cancelled shipments lack a recorded cancellation reason.")
    limit = 8 if intent.compact else 20
    keys = relevant_metrics(analysis, intent)
    if any(isinstance(analysis.metrics[k], list) and len(analysis.metrics[k]) > limit for k in keys) or (
            "financial" in intent.topics and any(isinstance(v, list) and len(v) > limit
            for v in analysis.metrics.get("shipping_financial", {}).values())):
        notes.append("Narrative comparisons cover a limited set of groups; omitted groups are not zero.")
    return notes


# Record text (courier names, reasons) is data. Text that reads like an instruction to the model is
# withheld from the AI context; Python tables and charts still show it as supplied.
_INSTRUCTION_LIKE = re.compile(r"\b(?:ignore|disregard|forget|override)\b.{0,40}\b(?:instructions?|rules?|prompts?|above|previous)\b"
                               r"|\b(?:system prompt|you are now|act as|new instructions?|respond with|return json)\b", re.I)


def neutralize(value):
    if isinstance(value, str):
        return "[text withheld]" if _INSTRUCTION_LIKE.search(value) else value
    if isinstance(value, dict):
        return {k: neutralize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [neutralize(v) for v in value]
    return value


def compact_context(analysis, intent):
    tables = selected_tables(analysis, intent)
    return {"metrics": neutralize(relevant_metrics(analysis, intent)), "scope": neutralize(analysis.metrics.get("scope", {})),
            "available_tables": {key: {"title": t.title, "columns": t.columns} for key, t in tables.items()},
            "limitations": [] if intent.omit_limitations else relevant_limitations(analysis, intent)}


def display_metrics(analysis, intent):
    m = analysis.metrics
    if intent.deterministic:
        return []
    if "financial" in intent.topics:
        f = m["shipping_financial"]
        entries = [("total_shipping_charges", "Total Shipping Charges", f["total_shipping_charges"] or "Unavailable", "text"),
                   ("average_charge_per_order", "Average Charge per Order", f["average_charge_per_order"] or "Unavailable", "text"),
                   ("cancelled_shipping_charges", "Charges Associated with Cancelled Orders", f["charges_associated_with_cancelled_orders"] or "Unavailable", "text"),
                   ("total_orders", "Orders Analyzed", analysis.records_analyzed, "number")]
    elif intent.status:
        entries = [("total_orders", f"{intent.status_label} Orders", analysis.records_analyzed, "number"),
                   ("distinct_couriers", "Couriers Used", m["distinct_couriers"], "number")]
        if analysis.records_analyzed:
            entries.append(("orders_with_valid_date", "Orders with Booking Date", m.get("order_date_range", {}).get("orders_with_valid_date", 0) if m.get("order_date_range") else 0, "number"))
    else:
        entries = [("total_orders", "Total Shipments", analysis.records_analyzed, "number")]
        counts = {c["category"]: c["count"] for c in m["status_category_breakdown"]}
        if "exception" in intent.topics:
            exc_count = counts.get("exception", 0) + counts.get("rto", 0)
            exc_rate = round((exc_count / max(analysis.records_analyzed, 1)) * 100, 1)
            entries += [("exceptions", "Exceptions / NDR", exc_count, "number"),
                        ("exception_rate", "Exception Rate", exc_rate, "percent")]
        elif "status" in intent.topics:
            entries += [("delivered", "Delivered Shipments", counts.get("delivered", 0), "number"),
                        ("delivered_percent", "Delivery Rate", m["rates"]["delivered_percent"], "percent")]
        if "cancellation" in intent.topics and "exception" not in intent.topics:
            entries += [("cancelled", "Cancelled Shipments", counts.get("cancelled", 0), "number"),
                        ("cancelled_percent", "Cancellation Rate", m["rates"]["cancelled_percent"], "percent")]
        if "courier" in intent.topics:
            entries.append(("distinct_couriers", "Couriers Used", m["distinct_couriers"], "number"))
    return [DisplayMetric(key=k, label=l, value=v, unit=u) for k, l, v, u in entries[:4]]


def report_charts(analysis, intent):
    if not intent.charts or not analysis.records_analyzed or "all" in getattr(intent, "excluded_charts", ()):
        return []
    m, charts = analysis.metrics, []
    def add(key, title, rows, label, value, unit, kind="bar"):
        rows = [r for r in rows if r.get(value) is not None]
        rows = sorted(rows, key=lambda r: float(r[value]), reverse=True)
        title += " (leading groups)" if len(rows) > 8 and kind == "bar" else ""
        rows = rows[:8]
        if rows:
            charts.append(ReportChart(id=key, title=title, type=kind, labels=[r[label] for r in rows],
                          datasets=[ChartDataset(label=unit, data=[float(r[value]) for r in rows])]))
    def has_multiple_slices(rows, val_key):
        return len([r for r in (rows or []) if r.get(val_key) is not None and float(r.get(val_key, 0)) > 0]) > 1

    status_breakdown = m.get("status_category_breakdown", [])
    has_multi_status = has_multiple_slices(status_breakdown, "count")

    # If the user explicitly specified charts (e.g. area chart for courier, line chart for status)
    if intent.chart_specs:
        added_topics = set()
        specs_to_use = [
            s for s in intent.chart_specs
            if s.get("type") not in getattr(intent, "excluded_charts", ())
            and s.get("topic") not in getattr(intent, "excluded_charts", ())
        ]
        for spec in specs_to_use:
            ctype = spec.get("type", "bar")
            topic = spec.get("topic")

            if topic == "courier" and "courier" not in added_topics and m.get("courier_performance"):
                desc_text = (spec.get("desc") or "").lower()
                wants_compare_deliv = (
                    any(w in desc_text for w in ["deliver", "delivery", "delivered"]) and
                    any(w in desc_text for w in ["volume", "total", "shipment", "orders", "count"])
                )
                if wants_compare_deliv:
                    rows = [r for r in m["courier_performance"] if r.get("total_orders") is not None]
                    rows = sorted(rows, key=lambda r: float(r["total_orders"]), reverse=True)[:8]
                    if rows:
                        charts.append(ReportChart(
                            id="courier_orders",
                            title="Courier Volume vs Delivery Counts",
                            type=ctype,
                            labels=[r["courier"] for r in rows],
                            datasets=[
                                ChartDataset(label="Total Volume", data=[float(r.get("total_orders", 0)) for r in rows]),
                                ChartDataset(label="Delivered", data=[float(r.get("delivered", 0)) for r in rows]),
                            ]
                        ))
                        added_topics.add("courier")
                else:
                    value = (intent.status if intent.status in ("delivered", "cancelled", "rto") else "total_orders") if intent.status else "cancelled" if "cancellation" in intent.topics else "total_orders"
                    if value == "cancelled" and all(float(r.get("cancelled", 0)) == 0 for r in m.get("courier_performance", [])):
                        value = "total_orders"
                    add("courier_orders", "Orders by Courier", m["courier_performance"], "courier", value, value.replace("_", " ").title(), ctype)
                    added_topics.add("courier")
            elif topic == "status" and "status" not in added_topics:
                if has_multi_status and not intent.status:
                    add("shipment_status", "Shipment Status Distribution", status_breakdown, "label", "count", "Orders", ctype)
                elif has_multiple_slices(m.get("account_status_mix"), "orders"):
                    add("account_status", "Account-Wide Status Mix", m["account_status_mix"], "status", "orders", "Account Orders", ctype)
                elif status_breakdown:
                    add("shipment_status", "Shipment Status Distribution", status_breakdown, "label", "count", "Orders", ctype)
                added_topics.add("status")
            elif topic == "financial" and "financial" not in added_topics and m.get("shipping_financial", {}).get("courier_charges"):
                cid = "courier_charges_pie" if ctype in {"pie", "doughnut", "donut"} else "courier_charges"
                add(cid, "Shipping Charges by Courier", m["shipping_financial"]["courier_charges"],
                    "courier", "total_shipping_charges", "Freight Charges", ctype)
                added_topics.add("financial")
            elif topic == "geography" and "geography" not in added_topics and m.get("top_destination_states"):
                add("destination_orders", "Orders by Destination State", m["top_destination_states"], "state", "count", "Orders", ctype)
                added_topics.add("geography")
            else:
                if "status" not in added_topics and (has_multi_status or status_breakdown):
                    add("shipment_status", "Shipment Status Distribution", status_breakdown, "label", "count", "Orders", ctype)
                    added_topics.add("status")
                elif "courier" not in added_topics and m.get("courier_performance"):
                    add("courier_orders", "Orders by Courier", m["courier_performance"], "courier", "total_orders", "Orders", ctype)
                    added_topics.add("courier")
                elif "financial" not in added_topics and m.get("shipping_financial", {}).get("courier_charges"):
                    add("courier_charges", "Shipping Charges by Courier", m["shipping_financial"]["courier_charges"],
                        "courier", "total_shipping_charges", "Freight Charges", ctype)
                    added_topics.add("financial")
        if charts:
            return charts[:1] if intent.single_chart else charts[:2]

    status_kind = "area" if intent.wants_area else "bar" if (intent.wants_bar and not (intent.donut or intent.both_circular)) else "doughnut" if intent.donut else "pie"
    circular_kind = "doughnut" if intent.donut else "pie"

    if "financial" in intent.topics and not (intent.donut or intent.chart_courier_charges):
        f = m["shipping_financial"]
        add("courier_charges", "Shipping Charges by Courier", f["courier_charges"], "courier", "total_shipping_charges", "Shipping charges (currency as supplied)", "area" if intent.wants_area else "bar")
        add("payment_charges", "Shipping Charges by Payment Mode", f["payment_mode_charges"], "payment_mode", "total_shipping_charges", "Shipping charges (currency as supplied)", "pie")
    else:
        # 1. Circular chart for Shipping Charges by Courier if requested
        if intent.chart_courier_charges and m.get("shipping_financial", {}).get("courier_charges"):
            kind = "pie" if intent.pie_courier else circular_kind
            add("courier_charges_pie", "Shipping Charges by Courier", m["shipping_financial"]["courier_charges"],
                "courier", "total_shipping_charges", "Freight Charges", kind)

        # 2. Donut / Status chart if donut requested or first/second chart needed
        if (intent.donut or len(charts) == 0) and not any(c.id in {"shipment_status", "account_status"} for c in charts):
            if has_multi_status and not intent.status:
                add("shipment_status", "Shipment Status Distribution", status_breakdown, "label", "count", "Orders", status_kind)
            elif has_multiple_slices(m.get("account_status_mix"), "orders"):
                add("account_status", "Account-Wide Status Mix", m["account_status_mix"], "status", "orders", "Account Orders", status_kind)
            elif has_multiple_slices(m.get("payment_mode_breakdown"), "count"):
                add("payment_orders", "Orders by Payment Mode", m["payment_mode_breakdown"], "payment_mode", "count", "Orders", circular_kind)
            elif not intent.status and status_breakdown:
                add("shipment_status", "Shipment Status Distribution", status_breakdown, "label", "count", "Orders", status_kind)

        # 3. Bar / circular / area chart: Courier Performance (if not already filled)
        if not intent.single_chart and len(charts) < 2 and "courier" in intent.topics and not intent.chart_courier_charges:
            value = (intent.status if intent.status in ("delivered", "cancelled", "rto") else "total_orders") if intent.status else "cancelled" if "cancellation" in intent.topics else "total_orders"
            if value == "cancelled" and all(float(r.get("cancelled", 0)) == 0 for r in m.get("courier_performance", [])):
                value = "total_orders"
            if intent.wants_area:
                kind = "area"
            elif intent.wants_bar and not intent.both_circular:
                kind = "bar"
            elif intent.pie_courier or (intent.both_circular and circular_kind in {"doughnut", "donut"}):
                kind = "pie"
            elif intent.both_circular or (intent.donut and not intent.wants_bar):
                kind = circular_kind
            else:
                kind = "bar"
            add("courier_orders", "Orders by Courier", m["courier_performance"], "courier", value, value.replace("_", " ").title(), kind)
        elif not intent.single_chart and len(charts) < 2 and "geography" in intent.topics:
            add("destination_orders", "Orders by Destination State", m["top_destination_states"], "state", "count", "Orders", "area" if intent.wants_area else "bar")

        # 4. Fill second chart if needed for detailed reports
        if not intent.single_chart and len(charts) == 1 and not intent.compact:
            if charts[0].type in {"bar", "area"}:
                if has_multi_status and not intent.status:
                    add("shipment_status", "Shipment Status Distribution", status_breakdown, "label", "count", "Orders", circular_kind)
                elif has_multiple_slices(m.get("account_status_mix"), "orders"):
                    add("account_status", "Account-Wide Status Mix", m["account_status_mix"], "status", "orders", "Account Orders", circular_kind)
                elif has_multiple_slices(m.get("payment_mode_breakdown"), "count"):
                    add("payment_orders", "Orders by Payment Mode", m["payment_mode_breakdown"], "payment_mode", "count", "Orders", circular_kind)
            elif charts[0].type in {"pie", "doughnut", "donut"}:
                if intent.wants_area:
                    kind = "area"
                elif intent.wants_bar and not intent.both_circular:
                    kind = "bar"
                elif intent.pie_courier or (intent.both_circular and charts[0].type in {"doughnut", "donut"}):
                    kind = "pie"
                elif intent.both_circular or (intent.donut and not intent.wants_bar):
                    kind = circular_kind
                else:
                    kind = "bar"
                if m.get("courier_performance") and not any(c.id == "courier_orders" for c in charts):
                    add("courier_orders", "Orders by Courier", m["courier_performance"], "courier", "total_orders", "Orders", kind)
                elif has_multiple_slices(m.get("payment_mode_breakdown"), "count"):
                    add("payment_orders", "Orders by Payment Mode", m["payment_mode_breakdown"], "payment_mode", "count", "Orders", kind)
    if getattr(intent, "excluded_charts", ()):
        excl = set(intent.excluded_charts)
        filtered = []
        for c in charts:
            ctype = (c.type or "").lower()
            cid = (c.id or "").lower()
            ctitle = (c.title or "").lower()
            if ctype in excl or cid in excl:
                continue
            if ("donut" in excl or "doughnut" in excl) and ctype in {"donut", "doughnut"}:
                continue
            if "courier" in excl and ("courier" in cid or "courier" in ctitle):
                continue
            if "status" in excl and ("status" in cid or "status" in ctitle):
                continue
            filtered.append(c)
        charts = filtered
    return charts[:1] if intent.single_chart else charts[:2]


def deterministic_narrative(analysis, intent):
    empty = intent.status and not analysis.records_analyzed
    text = (f"No {intent.status_label.lower()} shipments are available in the supplied data." if empty else
            "The requested comparisons cover the supplied shipment records.")
    return {"title": f"{intent.status_label} Shipment Dashboard" if empty else "Shipment Comparisons",
            "executive_summary": {"heading": "Overview", "paragraphs": [text]},
            "sections": [], "findings": [], "recommendations": [], "data_limitations": []}


def shape_narrative(raw, analysis, intent):
    """Select whole validated items; never cut off numbers or text mid-sentence."""
    from copy import deepcopy
    result = deepcopy(raw)
    from app.errors import AIResponseError
    references = [b["table_id"] for s in result.get("sections", []) for b in s.get("blocks", []) if b["type"] == "table_reference"]
    if len(references) != len(set(references)) or any(int(key.split("_")[1]) >= len(analysis.tables) for key in references):
        raise AIResponseError("The AI response contains an unknown or repeated table reference.")
    allowed = selected_tables(analysis, intent)
    if isinstance(result.get("executive_summary"), str):
        # Normalize the existing legacy response into the existing block schema.
        result["executive_summary"] = {"paragraphs": [result["executive_summary"]]}
        result["sections"] = [{"id": f"section_{i}", "title": s["heading"], "order": i + 1,
                               "blocks": [{"type": "paragraph", "text": s["content"]}]}
                              for i, s in enumerate(result.get("sections", []))]
    if intent.compact:
        summary = result["executive_summary"]
        summary["heading"] = "Overview"
        # One short complete paragraph; no repeated highlights alongside findings.
        summary["paragraphs"] = summary["paragraphs"][:1]
        if len(summary["paragraphs"][0]) > 350 or len(re.split(r"(?<=[.!?])\s+(?=[A-Z])", summary["paragraphs"][0])) > 2:
            summary["paragraphs"] = deterministic_narrative(analysis, intent)["executive_summary"]["paragraphs"]
        summary["highlights"] = []
        result["sections"] = []
        result["findings"] = [f for f in result.get("findings", []) if len(f["detail"]) <= 350
                              and len(re.split(r"(?<=[.!?])\s+(?=[A-Z])", f["detail"])) <= 2][:3]
    if not intent.recommendations or intent.deterministic:
        result["recommendations"] = []
    else:
        result["recommendations"] = [r for r in result.get("recommendations", [])
                                     if not intent.compact or len(r["action"]) + len(r.get("rationale", "")) <= 350][:2 if intent.compact else 5]
    seen = set()
    sections = []
    for section in result.get("sections", [])[:6]:
        blocks = []
        for block in section["blocks"]:
            if block["type"] == "table_reference":
                key = block["table_id"]
                if key not in allowed or key in seen:
                    continue
                seen.add(key)
            blocks.append(block)
        if blocks:
            section["blocks"] = blocks
            sections.append(section)
    # Python chooses relevant tables even if the AI omitted them; no table cells in AI input.
    for key, table in allowed.items():
        if key not in seen:
            sections.append({"id": "selected_" + key, "title": table.title, "order": len(sections) + 1,
                             "blocks": [{"type": "table_reference", "table_id": key}]})
    for i, section in enumerate(sections):
        section["order"] = i + 1
    result["sections"] = sections
    if intent.omit_limitations:
        result["data_limitations"] = []
    else:
        result["data_limitations"] = list(dict.fromkeys(relevant_limitations(analysis, intent)
                                          + result.get("data_limitations", [])[:1 if intent.compact else 2]))
    # Exact duplicate findings add no information.
    seen = set()
    result["findings"] = [f for f in result.get("findings", []) if not (f["detail"] in seen or seen.add(f["detail"]))]
    return result
