"""Source completeness and scope gate: runs before any metric, chart or AI call.

The selected filters (UI) and the source's own matched count are the contract. A report is only
built when the supplied records are the complete, de-duplicated set for that scope; otherwise the
request stops with an explicit error, unless the caller opted into a labelled partial report.

A filter being *applied at the source* and being *verified on the returned records* are recorded
separately: a field the records do not carry (e.g. manifest_datetime) is never checked against
another date field, and the scope is never labelled verified on that basis.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.pincodes import normalize_pincodes
from app.errors import ConflictingRecordsError, IncompleteSourceError, NoMatchingRecordsError, ScopeMismatchError
from app.prompts.shipment_prompt import (_DATE_FORMATS, FIELD_ALIASES, _flatten, _lookup, categorize_status,
                                         extract_orders, parse_date, replace_orders, scrub_text)

_IST = timezone(timedelta(hours=5, minutes=30))
# Statuses a selected label can be checked against; "other"/"unknown" labels are not verifiable.
_CHECKABLE = {"delivered", "in_transit", "pending", "cancelled", "rto", "exception", "lost"}
_ALL = {"", "all", "all orders", "all shipments", "all statuses", "any"}
# Record keys a selected date field may be verified against. Only the field itself: a missing
# manifest_datetime is never replaced by orderDate or createdAt.
_DATE_KEYS = {"manifest_datetime": ("manifestdatetime",)}
_TIME_FORMATS = tuple(f for f in _DATE_FORMATS if "%H" in f or "%I" in f)


@dataclass(frozen=True)
class SelectedFilters:
    """What the user selected in the UI; the source query must have used exactly this."""
    from_date: date | None = None
    to_date: date | None = None
    status_label: str | None = None
    date_field: str = "order_date"
    timezone: str = "Asia/Kolkata"
    pincodes: tuple[str, ...] = ()

    @property
    def status_selected(self) -> bool:
        return bool(self.status_label) and self.status_label.strip().lower() not in _ALL

    @property
    def status(self) -> str | None:
        """Status category the label selects, or None for All or a label that cannot be checked."""
        if not self.status_selected:
            return None
        category = categorize_status(self.status_label)
        return category if category in _CHECKABLE else None

    def to_dict(self) -> dict[str, Any]:
        return {"date_field": self.date_field, "timezone": self.timezone,
                "from_date": self.from_date.isoformat() if self.from_date else None,
                "to_date": self.to_date.isoformat() if self.to_date else None,
                "status": self.status_label or "All",
                **({"pincodes": list(self.pincodes)} if self.pincodes else {})}


@dataclass(frozen=True)
class SourceCheck:
    data: dict[str, Any]              # payload holding only the records the report may use
    completeness: dict[str, Any]
    limitations: tuple[str, ...]
    raw_records_json: str = "[]"  # Immutable request-local evidence; never serialized to clients or logs.


def _container(data: dict[str, Any]) -> dict[str, Any]:
    return data["data"] if isinstance(data.get("data"), dict) else data


def selected_filters(filters: Any, data: dict[str, Any]) -> SelectedFilters:
    """Explicit request filters win; otherwise the legacy ``data.report_filter`` sent by Angular."""
    if filters is not None:
        return SelectedFilters(from_date=filters.from_date, to_date=filters.to_date, status_label=filters.status,
                               date_field=filters.date_field, timezone=filters.timezone, pincodes=tuple(filters.pincodes))
    raw = _container(data).get("report_filter")
    raw = raw if isinstance(raw, dict) else {}
    return SelectedFilters(from_date=parse_date(raw.get("from_date")), to_date=parse_date(raw.get("to_date")),
                           status_label=scrub_text(raw.get("status_label") or raw.get("status"), 60),
                           date_field=str(raw.get("date_field") or "order_date"),
                           timezone=str(raw.get("timezone") or "Asia/Kolkata"),
                           pincodes=tuple(normalize_pincodes(raw.get("pincodes"))))


def with_report_filter(data: dict[str, Any], selected: SelectedFilters) -> dict[str, Any]:
    """The payload with ``report_filter`` set from the request, so every later stage reads one scope."""
    values = {"from_date": selected.from_date.isoformat() if selected.from_date else None,
              "to_date": selected.to_date.isoformat() if selected.to_date else None,
              "status_label": selected.status_label,
              "date_field": selected.date_field if selected.date_field != "order_date" else None,
              "timezone": selected.timezone,
              **({"pincodes": list(selected.pincodes)} if selected.pincodes else {})}
    values = {k: v for k, v in values.items() if v}
    if isinstance(data.get("data"), dict):
        return {**data, "data": {**data["data"], "report_filter": values}}
    return {**data, "report_filter": values}


def _zone(name: str):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return _IST


def _date_value(flat: dict[str, Any], field: str) -> Any:
    if field in FIELD_ALIASES:
        return _lookup(flat, field)
    return next((flat[k] for k in _DATE_KEYS.get(field, ()) if flat.get(k) not in (None, "")), None)


def _candidate_days(value: Any, zone) -> set[date]:
    """Calendar days the value can mean in the selected timezone.

    A timestamp with an offset (or an epoch) is one exact moment. A timestamp without an offset may be
    local time or UTC (undocumented), so near midnight it can mean two days; a bare date is one day.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        stamp = value / 1000 if value > 1e11 else value
        if 946684800 <= stamp <= 4102444800:  # 2000-01-01 .. 2100-01-01
            return {datetime.fromtimestamp(stamp, tz=timezone.utc).astimezone(zone).date()}
        return set()
    moment = None
    if isinstance(value, str) and ":" in value:
        text = value.strip()
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            for fmt in _TIME_FORMATS:
                try:
                    moment = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
    if moment is None:
        day = parse_date(value)
        return {day} if day else set()
    if moment.tzinfo:
        return {moment.astimezone(zone).date()}
    return {moment.date(), moment.replace(tzinfo=timezone.utc).astimezone(zone).date()}


def _id_part(value: Any) -> str:
    # Not scrub_text: its phone-number redaction would turn every 9-digit order ID into "[redacted]"
    # and merge different shipments. The key is internal only and never shown or sent to the AI.
    return re.sub(r"[^A-Za-z0-9_\-/]", "", str(value))[:60] if value is not None else ""


def _identity(flat: dict[str, Any]) -> tuple[str, str] | None:
    order_id, awb = _id_part(_lookup(flat, "order_id")), _id_part(_lookup(flat, "awb_number"))
    return (order_id, awb) if order_id or awb else None


def _expected(data: dict[str, Any], expected_records: int | None) -> int | None:
    if expected_records is not None:
        return expected_records
    from app.analysis.vocabulary import get_vocabulary
    return get_vocabulary("shipment").context(data).api_total


def _verification(selected: bool, checkable: bool, checked: int, total: int) -> str:
    if not selected:
        return "not_selected"
    if not checkable or not checked:
        return "not_verifiable"
    return "verified" if checked == total else "partially_verified"


def check_source(data: dict[str, Any], selected: SelectedFilters, *, expected_records: int | None = None,
                 allow_partial: bool = False, enforce: bool = True, rows_reconciled: bool = False,
                 source_owned: bool = False, source_info: dict[str, Any] | None = None) -> SourceCheck:
    """Reconcile expected vs fetched vs unique records and assert they match the selected scope.

    ``rows_reconciled``: the received row count was already matched to the source total page by page,
    so exact duplicates inside the source are removed without counting as missing rows.
    ``source_owned``: the backend fetched the rows with these filters; a failure here is a failure of
    the shipment service (HTTP 502), not of the request.
    """
    records = extract_orders(data)
    zone = _zone(selected.timezone)
    expected = _expected(data, expected_records)

    kept, seen = [], {}
    identical = conflicting = without_id = 0
    for record in records:
        key = _identity(_flatten(record))
        if key is None:
            without_id += 1
            kept.append(record)
            continue
        body = json.dumps(record, sort_keys=True, default=str)
        if key in seen:
            if body == seen[key]:
                identical += 1          # the same row twice: overlapping pages or a repeated cursor
            else:
                conflicting += 1        # another version of one shipment: never counted twice
            continue
        seen[key] = body
        kept.append(record)

    dated = selected.from_date is not None or selected.to_date is not None
    in_scope, mismatched = [], Counter()
    out_of_period = ambiguous = missing_dates = date_checked = status_checked = unverified = 0
    wanted = selected.status
    for record in kept:
        flat = _flatten(record)
        days = _candidate_days(_date_value(flat, selected.date_field), zone)
        inside = {(not selected.from_date or d >= selected.from_date) and (not selected.to_date or d <= selected.to_date)
                  for d in days}
        outside = False
        if not days:
            missing_dates += 1
        elif inside == {False}:
            out_of_period += 1
            date_checked += 1
            outside = True
        elif inside == {True}:
            date_checked += 1
        else:
            ambiguous += 1              # a time without timezone at a day boundary: not decidable
        if wanted:
            category = categorize_status(scrub_text(_lookup(flat, "status"), 80))
            if category not in _CHECKABLE:
                unverified += 1
            else:
                status_checked += 1
                if category != wanted:
                    mismatched[category] += 1
                    outside = True
        if not outside:
            in_scope.append(record)

    fetched, unique = len(records), len(kept)
    received = fetched if rows_reconciled else unique
    missing = max(expected - received, 0) if expected is not None else None
    out_of_scope = unique - len(in_scope)
    status = "unverified" if expected is None else "complete" if not missing and not out_of_scope else "partial"
    date_check = _verification(dated, True, date_checked, unique)
    status_check = _verification(selected.status_selected, wanted is not None, status_checked, unique)
    checks = {c for c in (date_check, status_check) if c != "not_selected"}
    completeness = {
        "status": status,
        "expected_records": expected,
        "fetched_records": fetched,
        "unique_records": unique,
        "duplicate_records_removed": identical,
        "conflicting_duplicates": conflicting,
        "records_without_id": without_id,
        "missing_records": missing,
        "outside_selected_period": out_of_period,
        "outside_selected_status": sum(mismatched.values()),
        "status_not_verifiable": unverified if wanted else None,
        "missing_dates": missing_dates,
        "analyzed_records": len(in_scope),
        "selected_filters": selected.to_dict(),
        "filters": {
            # True: the backend sent these filters to the source; None: the caller fetched the rows.
            "applied_at_source": True if source_owned else None,
            "date": {"field": selected.date_field, "verification": date_check, "checked_records": date_checked,
                     "missing_field_records": missing_dates, "ambiguous_records": ambiguous},
            "status": {"label": selected.status_label or "All", "verification": status_check,
                       "checked_records": status_checked, "not_verifiable_records": unverified if wanted else 0},
            **({"pincodes": {"values": list(selected.pincodes), "applied_at_source": False,
                             "verification": "deferred_to_analysis"}} if selected.pincodes else {}),
        },
        "scope_verification": ("no_filters" if not checks else "verified" if checks == {"verified"}
                               else "not_verifiable" if checks == {"not_verifiable"} else "partially_verified"),
        "data_as_of": (lambda d: d.isoformat() if d else None)(parse_date(_container(data).get("as_of") or data.get("as_of"))),
        **(source_info or {}),
    }

    upstream = {"status_code": 502, "retryable": True} if source_owned else {}
    if enforce and not allow_partial:
        if conflicting:  # first: the extra versions also make the unique count fall short
            raise ConflictingRecordsError(
                f"{conflicting:,} shipments arrived more than once with different values, so no report was created."
                + (" Please retry." if source_owned else ""), details=completeness, **upstream)
        if missing:
            raise IncompleteSourceError(
                f"Received {received:,} of {expected:,} matching shipments. Please retry." if source_owned else
                f"Only {received:,} of the {expected:,} matching shipments were received, so no report was created. "
                "Load all pages for the selected filters and try again.", details=completeness, **upstream)
        if out_of_scope:
            raise ScopeMismatchError(
                f"{out_of_scope:,} of the {unique:,} received shipments do not match the selected "
                + ("period and status" if out_of_period and mismatched else "period" if out_of_period else "status")
                + ", so the source query did not apply the selected filters. No report was created.",
                details=completeness, **({"status_code": 502} if source_owned else {}))
    if not in_scope:
        raise NoMatchingRecordsError("No shipments matched the selected dates and status.", details=completeness)

    field = selected.date_field.replace("_", " ")
    who = "shipment service" if source_owned else "source"
    notes = []
    if status == "unverified":
        notes.append("The source did not report how many shipments match the selected filters, so completeness "
                     f"could not be verified; figures cover the {len(in_scope):,} shipments received.")
    elif missing:
        notes.append(f"Partial report: {received:,} of {expected:,} matching shipments were received; "
                     "figures describe the received shipments only.")
    if out_of_scope:
        notes.append(f"{out_of_scope:,} received shipments outside the selected filters were excluded.")
    if identical:
        notes.append(f"{identical:,} duplicate shipment rows (same record received twice) were removed.")
    if conflicting:
        notes.append(f"{conflicting:,} shipments appeared more than once with different values; the first version "
                     "of each was used.")
    if dated and date_check == "not_verifiable":
        notes.append(f"The {field} filter was applied by the {who} but could not be checked here: the shipments "
                     f"do not include a {field} value.")
    elif dated and (missing_dates or ambiguous):
        notes.append(f"{missing_dates + ambiguous:,} shipments could not be checked against the selected period "
                     f"({missing_dates:,} without a {field}, {ambiguous:,} with a time but no timezone at a day boundary).")
    if selected.status_selected and wanted is None:
        notes.append(f"The status filter '{selected.status_label}' was applied by the {who} but cannot be checked "
                     "against shipment statuses here.")
    elif unverified and wanted:
        notes.append(f"{unverified:,} shipments have a status that could not be matched to the selected status.")
    return SourceCheck(data=replace_orders(data, in_scope) if len(in_scope) != fetched else data,
                       completeness=completeness, limitations=tuple(notes),
                       raw_records_json=json.dumps(records, sort_keys=True, default=str))
