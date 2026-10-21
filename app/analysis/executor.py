"""Apply a validated plan to the current request's rows. Every number here is calculated in Python."""
from __future__ import annotations

import re
import statistics
from collections import Counter
from datetime import date
from typing import Any

from app.analysis.plan import AnalysisPlan, Filter, TableSpec, Unsupported
from app.analysis.vocabulary import DataContext, PlanVocabulary
from app.charts.aggregate import date_buckets, group, measure
from app.charts.fields import DatasetAdapter
from app.models.report_schema import ReportTable, TableColumn

MISSING = "—"


def execute_ordered_sections(plan: AnalysisPlan, rows):
    """Execute the same validated AnalysisPlan's ordered coverage obligations."""
    from app.analysis.section_reports import execute_registered_section
    return [execute_registered_section(spec, rows) for spec in plan.sections]


# --- Filtering -----------------------------------------------------------------------

def _matches(value: Any, f: Filter) -> bool:
    if isinstance(f.value, (tuple, list)) and f.op in ("in", "not_in"):
        inside = str(value).casefold() in {str(v).casefold() for v in f.value}
        return inside if f.op == "in" else not inside
    if f.op in ("eq", "ne"):
        same = str(value).casefold() == str(f.value).casefold()
        return same if f.op == "eq" else not same
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    if f.op == "between":
        return f.value[0] <= value <= f.value[1]
    return {"gt": value > f.value, "gte": value >= f.value, "lt": value < f.value, "lte": value <= f.value}[f.op]


def matching_rows(rows: list[dict[str, Any]], plan: AnalysisPlan) -> tuple[list[dict[str, Any]], Counter]:
    """Rows inside the plan's scope, and how many were excluded only because a filtered value was missing."""
    kept, missing = [], Counter()
    for row in rows:
        ok = True
        for f in plan.filters:
            value = row.get(f.field)
            if value is None:
                missing[f.field] += 1
                ok = False
                break
            if not _matches(value, f):
                ok = False
                break
        if ok and plan.date_range:
            day = row.get(plan.date_range.field)
            if not isinstance(day, date):
                missing[plan.date_range.field] += 1
                ok = False
            elif not plan.date_range.start <= day <= plan.date_range.end:
                ok = False
        if ok:
            kept.append(row)
    return kept, missing


# --- Table formatting -----------------------------------------------------------------

def _display(value: Any, kind: str, decimals: int | None) -> str:
    if value is None or value == "":
        return MISSING
    if isinstance(value, date):
        return value.isoformat()
    if kind in ("currency", "number", "percent") and isinstance(value, (int, float)) and not isinstance(value, bool):
        places = 2 if decimals is None and kind == "currency" else decimals or 0
        text = f"{value:,.{places}f}"
        return text
    return str(value)


def _typed(value: Any, kind: str) -> float | int | str | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value.isoformat()
    if kind in ("currency", "number", "percent") and isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value) if float(value).is_integer() and kind == "number" else round(float(value), 4)
    return str(value)


def make_table(title: str, columns: list[tuple[str, str, str, int | None]], records: list[list[Any]], *,
               table_id: str | None = None, total_rows: int | None = None, note: str | None = None) -> ReportTable:
    """columns: (key, label, type, decimals). Display strings and typed values come from the same numbers."""
    meta = [TableColumn(key=key, label=label, type=kind, decimals=decimals,
                        align="right" if kind in ("number", "currency", "percent") else "left")
            for key, label, kind, decimals in columns]
    rows = [[_display(v, c[2], c[3]) for v, c in zip(record, columns)] for record in records]
    values = [[_typed(v, c[2]) for v, c in zip(record, columns)] for record in records]
    total = len(records) if total_rows is None else total_rows
    return ReportTable(title=title[:200], columns=[c[1] for c in columns], rows=rows, values=values, column_meta=meta,
                       id=table_id, total_rows=total, truncated=total > len(records), note=note)


_PERCENT_HEADER = re.compile(r"\(%\)|\brate\b|\bshare\b|percent", re.I)
_MONEY_HEADER = re.compile(r"charges?\b|amount|value|freight|average charge", re.I)
_COUNT_HEADER = re.compile(r"orders|count|charges$|valid charges|delivered|cancelled|rto|shipments|records", re.I)


def _cell_number(cell: str) -> float | None:
    text = cell.replace(",", "").replace("%", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def annotate_table(table: ReportTable) -> ReportTable:
    """Column types, alignment and typed values for an existing (string-celled) table; cells are unchanged."""
    if table.column_meta is not None:
        return table
    meta, kinds = [], []
    for i, label in enumerate(table.columns):
        cells = [row[i] for row in table.rows if row[i] not in ("", MISSING, "Unavailable")]
        numeric = bool(cells) and all(_cell_number(c) is not None for c in cells)
        if not numeric:
            kind = "text"
        elif _PERCENT_HEADER.search(label):
            kind = "percent"
        elif _MONEY_HEADER.search(label) and not re.search(r"valid charges", label, re.I):
            kind = "currency"
        else:
            kind = "number"
        decimals = 1 if kind == "percent" else 2 if kind == "currency" else None
        kinds.append(kind)
        meta.append(TableColumn(key=re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:60] or f"column_{i}",
                                label=label, type=kind, decimals=decimals,
                                align="right" if kind != "text" else "left"))
    values = []
    for row in table.rows:
        typed = []
        for cell, kind in zip(row, kinds):
            number = _cell_number(cell) if kind != "text" else None
            typed.append(None if cell in (MISSING, "Unavailable", "") else
                         (int(number) if number is not None and kind == "number" and number.is_integer() else number)
                         if kind != "text" else cell)
        values.append(typed)
    return table.model_copy(update={"column_meta": meta, "values": values,
                                    "total_rows": table.total_rows if table.total_rows is not None else len(table.rows)})


# --- Plan tables --------------------------------------------------------------------------

def _detail_table(spec: TableSpec, rows: list[dict[str, Any]], adapter: DatasetAdapter, vocab: PlanVocabulary,
                  requested: set[str]) -> tuple[ReportTable | None, list[str]]:
    notes = []
    columns = [c for c in spec.columns if c == vocab.id_column or any(r.get(c) not in (None, "") for r in rows)]
    dropped = [vocab.label(c, adapter) for c in spec.columns if c not in columns and c in requested]
    if dropped:
        notes.append(f"No {', '.join(d.lower() for d in dropped)} values are recorded for the matching records.")
    if not rows:
        return None, notes
    key = spec.sort
    ordered = list(rows)
    if key:  # records missing the sort value go last, never dropped
        ordered = (sorted((r for r in rows if r.get(key) is not None), key=lambda r: r[key], reverse=spec.descending)
                   + [r for r in rows if r.get(key) is None])
    limit = spec.limit or vocab.max_table_rows
    shown = ordered[:limit]
    spec_cols = [(c, vocab.label(c, adapter), vocab.column_type(c, adapter), vocab.detail_columns[c].decimals
                  if c in vocab.detail_columns else None) for c in columns]
    note = None
    if len(ordered) > limit:
        note = (f"Showing {limit:,} of {len(ordered):,} matching records"
                + (f", sorted by {vocab.label(key, adapter).lower()} ("
                   + ("newest" if vocab.column_type(key, adapter) == "date" else "highest") + " first)" if key else "") + ".")
    table = make_table(spec.title, spec_cols, [[r.get(c) for c in columns] for r in shown], table_id="plan_detail",
                       total_rows=len(ordered), note=note)
    return table, notes


def _grouped_table(spec: TableSpec, rows: list[dict[str, Any]], adapter: DatasetAdapter, vocab: PlanVocabulary,
                   index: int) -> ReportTable | None:
    if not rows:
        return None
    # A grouping whose field no record carries would be one "Not specified" row; the plan reports it as missing.
    if any(name and not any(r.get(name) not in (None, "") for r in rows) for name in (spec.group_by, spec.series)):
        return None
    field = adapter.fields[spec.group_by]
    measures = [adapter.measures[m] for m in spec.columns]
    if field.kind == "date":
        _, buckets = date_buckets(rows, field.name, spec.granularity, vocab.max_table_rows)
        groups = [(label, items) for label, items in buckets]
    else:
        groups = list(group(rows, field.name, adapter).items())
    if not groups:
        return None
    total = len(rows)
    series = adapter.fields[spec.series] if spec.series else None
    if field.kind != "date":
        groups.sort(key=lambda g: -len(g[1]))
    records = []
    for label, items in groups:
        # Cross-tab: one row per (group, series value). Every measure, including rates, is calculated over
        # that cell's own records; the share is of the group's records (e.g. COD share of Delhi's orders).
        cells = list(group(items, series.name, adapter).items()) if series else [(None, items)]
        cells.sort(key=lambda c: -len(c[1]))
        for series_label, cell in cells:
            values: list[Any] = [label] + ([series_label] if series else [])
            for m in measures:
                values.append(measure(cell, m))
                if m.name == adapter.default_measure:
                    base = len(items) if series else total
                    values.append(round(100.0 * len(cell) / base, 1) if base else 0.0)
            records.append(values)
    if field.kind != "date" and not series:
        records.sort(key=lambda r: (r[1] is None, -(r[1] or 0)))
    columns = [(field.name, field.label, "date" if field.kind == "date" else "text", None)]
    if series:
        columns.append((series.name, series.label, "text", None))
    for m in measures:
        kind = "percent" if m.value_format == "percent" else "currency" if m.value_format == "currency" else "number"
        columns.append((m.name, m.label + (" (%)" if kind == "percent" else ""), kind,
                        1 if kind == "percent" else 2 if kind == "currency" or m.agg == "avg" else 0))
        if m.name == adapter.default_measure:
            columns.append(("share_percent", f"Share of {field.label} (%)" if series else "Share (%)", "percent", 1))
    limit = spec.limit or vocab.max_table_rows
    note = f"Showing {limit:,} of {len(records):,} groups; groups not shown are not zero." if len(records) > limit else None
    if series:
        rates = [m.label for m in measures if m.agg == "rate"]
        verb = "uses" if len(rates) == 1 else "use"
        note = " ".join(filter(None, [note, (f"{', '.join(rates)} {verb} each row's own orders as the denominator; "
                                             if rates else "") + f"share is of each {field.label.lower()}'s orders."]))
    return make_table(spec.title, columns, records[:limit], table_id=f"plan_grouped_{index}", total_rows=len(records),
                      note=note)


def _duration_table(spec: TableSpec, rows: list[dict[str, Any]], adapter: DatasetAdapter, vocab: PlanVocabulary
                    ) -> tuple[ReportTable | None, list[str], list[Unsupported]]:
    """Days from order to delivery for delivered records whose timestamps are consistent; conflicts are flagged."""
    status = vocab.status_field
    delivered = [r for r in rows if r.get(status) == "delivered"]
    with_dates = [r for r in delivered if isinstance(r.get("delivery_date"), date) and isinstance(r.get("order_date"), date)]
    conflicts = [r for r in with_dates if r["delivery_date"] < r["order_date"]
                 or (isinstance(r.get("pickup_date"), date) and (r["pickup_date"] < r["order_date"]
                                                                  or r["delivery_date"] < r["pickup_date"]))]
    open_with_date = sum(1 for r in rows if r.get(status) != "delivered" and isinstance(r.get("delivery_date"), date))
    valid = [r for r in with_dates if r not in conflicts]
    notes = []
    if conflicts:
        notes.append(f"{len(conflicts):,} delivered records have conflicting timestamps (delivery or pickup before the "
                     "order date, or delivery before pickup) and are excluded from delivery-duration figures.")
    if open_with_date:
        notes.append(f"{open_with_date:,} records have a delivery date but a status other than delivered; they are not "
                     "counted as delivered.")
    if len(with_dates) < len(delivered):
        notes.append(f"{len(delivered) - len(with_dates):,} delivered records lack an order or delivery date.")
    if not valid:
        reason = ("No delivered records have consistent order and delivery dates, so delivery duration cannot be "
                  "calculated reliably.")
        return None, notes, [Unsupported("delivery duration", reason, ("Delivery Date",))]
    def stats(items):
        days = [(r["delivery_date"] - r["order_date"]).days for r in items]
        return [len(items), round(statistics.fmean(days), 1), float(statistics.median(days)), float(max(days))]
    records = [["All couriers", *stats(valid)]]
    for label, items in group(valid, spec.group_by, adapter).items():
        records.append([label, *stats(items)])
    columns = [(spec.group_by, adapter.fields[spec.group_by].label, "text", None),
               ("delivered_with_valid_dates", "Delivered (valid dates)", "number", 0),
               ("average_days", "Average Days", "number", 1), ("median_days", "Median Days", "number", 1),
               ("max_days", "Max Days", "number", 0)]
    note = "Days from order date to delivery date; delivered records with consistent timestamps only."
    return make_table(spec.title, columns, records, table_id="plan_duration", note=note), notes, []


def build_plan_tables(plan: AnalysisPlan, rows: list[dict[str, Any]], adapter: DatasetAdapter, vocab: PlanVocabulary,
                      requested_columns: set[str] | None = None) -> tuple[list[ReportTable], list[str], list[Unsupported]]:
    tables, notes, unsupported = [], [], []
    for i, spec in enumerate(plan.tables):
        if spec.kind == "detail":
            table, extra = _detail_table(spec, rows, adapter, vocab, requested_columns or set())
            notes += extra
        elif spec.kind == "grouped":
            table = _grouped_table(spec, rows, adapter, vocab, i)
        else:
            table, extra, missing = _duration_table(spec, rows, adapter, vocab)
            notes += extra
            unsupported += missing
        if table is not None:
            tables.append(table)
    return tables, notes, unsupported


# --- Dataset scope ------------------------------------------------------------------------

def _day(value: date) -> str:
    return f"{value.day} {value.strftime('%b %Y')}"


def dataset_scope(context: DataContext, plan: AnalysisPlan | None, adapter: DatasetAdapter, vocab: PlanVocabulary,
                  all_rows: list[dict[str, Any]], scoped_rows: list[dict[str, Any]], missing: Counter) -> dict[str, Any]:
    """Records supplied vs. reported by the API vs. analysed, stated plainly; never assumes completeness."""
    supplied, analysed = context.records_supplied, len(scoped_rows)
    noun = vocab.record_noun
    dates = [r[vocab.date_field] for r in all_rows if vocab.date_field and isinstance(r.get(vocab.date_field), date)]
    period = {"from": min(dates).isoformat(), "to": max(dates).isoformat()} if dates else None
    reported = max(t for t in (context.api_total, context.account_total, supplied) if t is not None)
    filters = plan.describe_scope(adapter, vocab) if plan else []
    statement = [f"This report covers {analysed:,} of {supplied:,} supplied {noun} records"
                 + (f" matching {'; '.join(filters)}." if filters else ".")]
    if context.api_total is not None and context.api_total > supplied:
        statement.append(f"The API reports {context.api_total:,} records for this query but only {supplied:,} were supplied "
                         "(partial or paginated response); figures describe the supplied records only.")
    elif context.account_total is not None and context.account_total > supplied:
        statement.append(f"The account summary counts {context.account_total:,} orders in total; figures describe the "
                         f"{supplied:,} supplied records only.")
    if context.api_filter:
        described = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in context.api_filter.items())
        statement.append(f"The supplied records were fetched with: {described}.")
    if plan and plan.date_range and context.api_period:
        start, end = context.api_period
        if plan.date_range.start < start or plan.date_range.end > end:
            statement.append(f"The requested period ({plan.date_range.describe()}) extends beyond the period the data was "
                             f"fetched for ({_day(start)} to {_day(end)}); records outside it were not supplied, so results "
                             "for the requested period may be incomplete.")
    # Record dates are checked against the period the API says it returned and the period the user asked for.
    outside_api = outside_requested = 0
    if dates and context.api_period:
        start, end = context.api_period
        outside_api = sum(1 for d in dates if not start <= d <= end)
        if outside_api:
            statement.append(f"{outside_api:,} supplied record{' has' if outside_api == 1 else 's have'} an order date "
                             "outside the API filter period "
                             f"({_day(start)} to {_day(end)}); they are flagged as out of range"
                             + ("." if plan and plan.date_range else " and included as supplied."))
    if dates and plan and plan.date_range:
        outside_requested = sum(1 for d in dates if not plan.date_range.start <= d <= plan.date_range.end)
        if outside_requested:
            statement.append(f"{outside_requested:,} supplied record{' falls' if outside_requested == 1 else 's fall'} "
                             "outside the requested period "
                             f"({plan.date_range.describe()}) and were excluded.")
    for name, count in missing.items():
        statement.append(f"{count:,} records without a {vocab.label(name, adapter).lower()} were excluded by the filter.")
    return {
        "records_outside_api_period": outside_api if context.api_period else None,
        "records_outside_requested_period": outside_requested if plan and plan.date_range else None,
        "records_supplied": supplied,
        "api_reported_total": context.api_total,
        "account_total_orders": context.account_total,
        "records_after_filters": analysed,
        "is_partial": reported > supplied,
        "api_filter": context.api_filter or None,
        "requested_filters": filters,
        "requested_period": None if not (plan and plan.date_range) else {
            "from": plan.date_range.start.isoformat(), "to": plan.date_range.end.isoformat(),
            "requested": plan.date_range.label, "basis": plan.date_range.basis},
        "supplied_data_period": period,
        "excluded_missing_values": dict(missing) or None,
        "data_fingerprint": context.fingerprint,
        "statement": " ".join(statement),
    }
