"""Request-local reference calculations and the final customer-response accuracy gate.

The source snapshot is immutable JSON. No source calls, AI calls, or analytics builders
are used here. Field aliases/status definitions are shared business rules, not totals.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import os
import tempfile
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_HALF_EVEN
from typing import Any

from app.errors import ReportAccuracyError, ReportRevisionError, request_id_var
from app.prompts.shipment_prompt import (
    _flatten, _lookup, categorize_status, extract_orders, normalize_payment, parse_date, scrub_text,
)

logger = logging.getLogger("ai_box.report_accuracy")


def _load_signing_key():
    configured = os.environ.get("REPORT_VALIDATION_SIGNING_KEY", "").strip()
    if configured:
        if len(configured.encode()) < 32:
            raise ValueError("REPORT_VALIDATION_SIGNING_KEY must have at least 32 bytes")
        return configured.encode()
    from app.config import settings
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    path = settings.output_dir / ".report_revision.key"
    if not path.exists():
        # Link a fully written private file atomically; concurrent workers read the winner.
        with tempfile.NamedTemporaryFile(dir=settings.output_dir, prefix="revision-", suffix=".tmp", delete=False) as handle:
            temp = handle.name
            handle.write(secrets.token_bytes(32))
        try:
            os.link(temp, path)
        except FileExistsError:
            pass
        finally:
            os.unlink(temp)
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError("Stored report revision signing key is invalid")
    return key


_SIGNING_KEY = _load_signing_key()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _immutable(*args, **kwargs):
    raise TypeError("Shipment source snapshots are read-only")


class _FrozenDict(dict):
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable

    def __deepcopy__(self, memo):
        return self


class _FrozenList(list):
    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = reverse = sort = __iadd__ = __imul__ = _immutable

    def __deepcopy__(self, memo):
        return self


def immutable_data(value):
    """Retain dict/list compatibility with existing readers, while prohibiting source writes."""
    if isinstance(value, dict):
        return _FrozenDict((k, immutable_data(v)) for k, v in value.items())
    if isinstance(value, list):
        return _FrozenList(immutable_data(v) for v in value)
    return value


def number(value):
    if value is None or isinstance(value, bool) or str(value).strip().lower() in {"", "unavailable", "none", "null", "—"}:
        return None
    try:
        import re
        result = Decimal(re.sub(r"(?i)inr|rs\.?|₹|,|%|\s", "", str(value)))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def equivalent(actual, expected, decimals=0):
    actual, expected = number(actual), number(expected)
    if actual is None or expected is None:
        return actual is expected
    if decimals == 0:
        return actual == expected
    quantum = Decimal(1).scaleb(-decimals)
    rounding = ROUND_HALF_EVEN if decimals == 1 else ROUND_HALF_UP
    return actual.quantize(quantum, rounding=rounding) == expected.quantize(quantum, rounding=rounding)


def reference_row(record, timezone_name="Asia/Kolkata"):
    flat = _flatten(record)
    row = {}
    for key, limit in (("courier", 80), ("destination_city", 60), ("destination_state", 60),
                       ("order_id", 40), ("awb_number", 40), ("cancellation_reason", 160), ("rto_reason", 160),
                       ("courier_remarks", 160), ("status", 80)):
        row[key] = scrub_text(_lookup(flat, key), limit)
    # Verify the documented nested source fields even if the production alias map regresses.
    consignee = record.get("consignee")
    transit = record.get("domestic_transit_stats")
    if isinstance(consignee, dict):
        for key in ("city", "state"):
            if consignee.get(key) not in (None, ""):
                row["destination_" + key] = scrub_text(consignee[key], 60)
    row["status_category"] = categorize_status(row["status"])
    row["payment_mode"] = normalize_payment(scrub_text(_lookup(flat, "payment_mode"), 40))
    for key in ("order_date", "pickup_date", "delivery_date"):
        raw = _lookup(flat, key)
        nested_key = {"pickup_date": "date_pickup", "delivery_date": "date_delivery"}.get(key)
        if isinstance(transit, dict) and nested_key and transit.get(nested_key) not in (None, ""):
            raw = transit[nested_key]
        row[key] = parse_date(raw)
    if isinstance(transit, dict) and transit.get("courier_remarks"):
        row["courier_remarks"] = scrub_text(transit["courier_remarks"], 160)
    for key in ("charge", "order_value", "weight", "collectable_amount"):
        value = number(_lookup(flat, key))
        row[key] = value if value is not None and value >= 0 else None
    row["zone"] = scrub_text(_lookup(flat, "zone"), 30)
    row["weekday"] = row["order_date"].strftime("%A") if row["order_date"] else None
    from app.prompts.shipment_prompt import manifest_calendar_day, normalize_shipment_status, extended_shipment_fields
    row.update(extended_shipment_fields(record))
    row["manifest_datetime"] = manifest_calendar_day(record, timezone_name)
    row["shipment_status"] = normalize_shipment_status(row["status"])
    return row


@dataclass(frozen=True)
class DatasetSnapshot:
    data_json: str
    raw_json: str
    completeness_json: str
    filters_json: str
    request_id: str
    account_scope: str
    prompt_hash: str
    fingerprint: str

    @classmethod
    def capture(cls, data, source=None, selected=None, user_id=None, prompt=""):
        frozen = canonical(data)
        records = extract_orders(json.loads(frozen))
        return cls(frozen, source.raw_records_json if source else canonical(records),
                   canonical(source.completeness if source else {}),
                   canonical(selected.to_dict() if selected else {}),
                   request_id_var.get() or secrets.token_hex(16), digest(user_id or "payload_scope")[:20],
                   digest(prompt), digest(records))

    def records(self):
        return extract_orders(json.loads(self.data_json))


@dataclass
class Audit:
    snapshot: DatasetSnapshot
    report_id: str
    checks: list[dict] = field(default_factory=list)
    stages: dict = field(default_factory=dict)

    def add(self, check_id, ok, expected=None, actual=None, reference="report", explanation=""):
        self.checks.append({"check_id": check_id, "status": "WARN" if ok is None else "PASS" if ok else "FAIL",
                            "expected": expected, "actual": actual, "reference": reference,
                            "explanation": explanation})

    def finish(self, *, block=True):
        counts = Counter(c["status"] for c in self.checks)
        event = {"event": "report_accuracy", "request_id": self.snapshot.request_id,
                 "report_id": self.report_id, "dataset_fingerprint": self.snapshot.fingerprint,
                 "raw_fingerprint": digest(json.loads(self.snapshot.raw_json)),
                 "stages": self.stages,
                 "account_scope": self.snapshot.account_scope, "prompt_hash": self.snapshot.prompt_hash,
                 "checks": self.checks, "passed": counts["PASS"], "failed": counts["FAIL"], "warnings": counts["WARN"],
                 "blocking": bool(block and counts["FAIL"])}
        lines = [f"[REPORT CHECK] request_id={self.snapshot.request_id} report_id={self.report_id} dataset={self.snapshot.fingerprint}"]
        try:
            "✓ ✗".encode(getattr(sys.stderr, "encoding", None) or "ascii")
            marks = {"PASS": "✓ PASS", "FAIL": "✗ FAIL", "WARN": "! WARN"}
        except (UnicodeEncodeError, LookupError):
            marks = {s: f"[{s}]" for s in ("PASS", "FAIL", "WARN")}
        # Compact repeated successful cell checks; retain every failed/unverified reference.
        grouped = Counter(c["check_id"] for c in self.checks if c["status"] == "PASS")
        emitted = set()
        for check in self.checks:
            if check["status"] == "PASS" and check["check_id"] in emitted:
                continue
            emitted.add(check["check_id"]) if check["status"] == "PASS" else None
            suffix = f" ({grouped[check['check_id']]} values checked)" if grouped[check["check_id"]] > 1 and check["status"] == "PASS" else ""
            lines.append(f"{marks[check['status']]} {check['check_id']} expected={canonical(check['expected'])} "
                         f"actual={canonical(check['actual'])} ref={check['reference']} {check['explanation']}{suffix}")
        result = "FAILED" if counts["FAIL"] else "PASSED"
        delivery = " delivery=allowed policy=audit_only" if counts["FAIL"] and not block else ""
        lines.append(f"[RESULT] {result} checks_passed={counts['PASS']} "
                     f"checks_failed={counts['FAIL']} warnings={counts['WARN']}{delivery}")
        # One logging record holds the entire block, so concurrent requests cannot interleave lines.
        logger.log(logging.ERROR if counts["FAIL"] else logging.INFO, "\n".join(lines), extra={"report_check": event})
        if counts["FAIL"] and block:
            raise ReportAccuracyError("The report did not pass accuracy checks. Please retry or contact support.",
                                      details={"request_id": self.snapshot.request_id})
        return event


def _pct(count, total):
    return Decimal(count) * 100 / total if total else Decimal(0)


def _stats(rows, key):
    values = sorted(r[key] for r in rows if r.get(key) is not None)
    if not values:
        return {"count": 0, "sum": None, "avg": None, "median": None}
    return {"count": len(values), "sum": sum(values, Decimal(0)),
            "avg": sum(values, Decimal(0)) / len(values),
            # Documented v3 dashboard definition: sorted[n // 2], including even cohorts.
            "median": values[len(values) // 2]}


def _matches(row, condition):
    actual, value, op = row.get(condition.field), condition.value, condition.op
    if actual is None:
        return False
    if isinstance(actual, date) and op in {"gt", "gte", "lt", "lte", "between"}:
        bounds = [date.fromisoformat(v) if isinstance(v, str) else v for v in (value if op == "between" else [value])]
        if op == "between":
            return bounds[0] <= actual <= bounds[1]
        return {"gt": lambda: actual > bounds[0], "gte": lambda: actual >= bounds[0],
                "lt": lambda: actual < bounds[0], "lte": lambda: actual <= bounds[0]}[op]()
    if op in ("eq", "ne"):
        return (str(actual).casefold() == str(value).casefold()) == (op == "eq")
    if op in ("in", "not_in"):
        return (str(actual).casefold() in {str(v).casefold() for v in value}) == (op == "in")
    if op == "between":
        return value[0] <= actual <= value[1]
    return {"gt": lambda: actual > value, "gte": lambda: actual >= value,
            "lt": lambda: actual < value, "lte": lambda: actual <= value}[op]()


def subset(rows, plan):
    if not plan:
        return rows
    return [r for r in rows if all(_matches(r, f) for f in plan.filters)
            and (not plan.date_range or r.get(plan.date_range.field) is not None
                 and plan.date_range.start <= r[plan.date_range.field] <= plan.date_range.end)]


def _check_value(audit, check_id, actual, expected, decimals=0, reference="report"):
    valid_missing = actual is None or str(actual).strip().lower() in {"unavailable", "none", "null", "", "—"}
    ok = valid_missing if expected is None else equivalent(actual, expected, decimals)
    audit.add(check_id, ok, expected, number(actual), reference,
              f"precision={decimals}")


def _label(field, value):
    if value is None or value == "":
        return "Not specified"
    if field == "status_category":
        from app.prompts.shipment_prompt import _CATEGORY_LABELS
        return _CATEGORY_LABELS.get(value, str(value))
    return str(value)


def _dimension(table):
    keys = [c.get("key") for c in table.get("column_meta") or []]
    if keys and keys[0] in {"courier", "destination_city", "destination_state", "status_category", "payment_mode", "rto_reason"}:
        return keys[0]
    first = (table.get("columns") or [""])[0].lower()
    return next((field for word, field in (("courier", "courier"), ("city", "destination_city"),
                ("state", "destination_state"), ("status", "status_category"), ("payment", "payment_mode")) if word in first), None)


def _measure(rows, key, adapter):
    if key in {"count", "orders", "total_orders"}:
        return len(rows)
    spec = adapter.measures.get(key) if adapter else None
    if not spec:
        return None
    selected = [r for r in rows if all(r.get(k) in v for k, v in spec.where)]
    if spec.agg == "count":
        return len(selected)
    if spec.agg == "rate":
        return _pct(len(selected), len(rows))
    values = [r[spec.field] for r in rows if r.get(spec.field) is not None]
    if not values:
        return None
    return sum(values, Decimal(0)) / (len(values) if spec.agg == "avg" else 1)


def _tables(public):
    tables = list(public.get("tables") or [])
    structured = public.get("structured_report") or {}
    for section in structured.get("sections") or []:
        tables.extend(b for b in section.get("blocks") or [] if b.get("type") == "table")
    return tables


def check_table(audit, table, rows, index, adapter=None, plan=None):
    reference = f"tables[{index}]"
    if table.get("values") is not None:
        for j, (typed, display) in enumerate(zip(table["values"], table.get("rows") or [])):
            for k, (value, text) in enumerate(zip(typed, display)):
                meta = (table.get("column_meta") or [{}] * len(typed))[k]
                if meta.get("type") in {"number", "currency", "percent"}:
                    _check_value(audit, "table_display_value", text, value, meta.get("decimals") or 0,
                                 f"{reference}.display[{j}][{k}]")
                else:
                    audit.add("table_display_label", str(value) == str(text) or value is None and text in {"—", "", "Unavailable"},
                              True, str(value) == str(text), f"{reference}.display[{j}][{k}]", "labels withheld")
    if table.get("id") == "plan_duration":
        return  # Checked independently against delivered dates by _coverage.
    if (table.get("id") or "").startswith("section_"):
        return  # Independent ordered-section coverage validator checks the section's exact cohort and values.
    if table.get("id") == "plan_detail":
        _check_value(audit, "detail_total_rows", table.get("total_rows"), len(rows), reference=reference)
        expected_ids = {str(r.get("order_id")) for r in rows if r.get("order_id")}
        keys = [c["key"] for c in table.get("column_meta") or []]
        if "order_id" in keys:
            position = keys.index("order_id")
            shown = [str(v[position]) for v in (table.get("values") or table.get("rows") or [])]
            audit.add("detail_identities", all(key in expected_ids for key in shown), True,
                      all(key in expected_ids for key in shown), reference, "shipment IDs withheld")
        if len(table.get("rows") or []) < len(rows):
            audit.add("detail_truncation", bool(table.get("truncated") and table.get("note")),
                      "declared", "declared" if table.get("truncated") and table.get("note") else "silent", reference)
        return
    if table.get("title") == "Account-Level Status Benchmark":
        audit.add("account_benchmark", None, reference=reference,
                  explanation="Source-account counters; separate from fetched and analysis cohorts")
        return
    dimension = _dimension(table)
    if not dimension:
        audit.add("table_unverified", None, reference=reference,
                  explanation="No independent categorical mapping; not claimed verified")
        return
    groups = defaultdict(list)
    for row in rows:
        groups[_label(dimension, row.get(dimension)).casefold()].append(row)
    data = table.get("values") if table.get("values") is not None else table.get("rows", [])
    meta = table.get("column_meta") or []
    columns = table.get("columns") or []
    shown = set()
    series_key = meta[1].get("key") if len(meta) > 1 else None
    categorical = {"payment_mode", "courier", "status_category", "destination_city", "destination_state"}
    labels = [(str(v[0]).casefold(), str(v[1]).casefold() if series_key in categorical else "") for v in data]
    audit.add("table_unique_categories", len(labels) == len(set(labels)), len(labels), len(set(labels)), reference,
              "group/series categories must be unique")
    for j, values in enumerate(data):
        label = str(values[0]).casefold()
        shown.add(label)
        if label == "other" and label not in groups:
            continue
        audit.add("table_category", label in groups, True, label in groups, f"{reference}.rows[{j}]",
                  "category membership; names withheld")
        group = groups.get(label, [])
        denominator = len(rows)
        if len(meta) > 1 and meta[1].get("key") in {"payment_mode", "courier", "status_category", "destination_city", "destination_state"}:
            series = meta[1]["key"]
            denominator = len(group)
            group = [r for r in group if _label(series, r.get(series)).casefold() == str(values[1]).casefold()]
        for k, cell in enumerate(values[1:], 1):
            heading = columns[k].lower()
            key = meta[k].get("key") if len(meta) > k else None
            expected, precision = None, 0
            if key in {"share_percent", "percent"} or "share" in heading:
                expected, precision = _pct(len(group), denominator), 1
            elif key and adapter and key in adapter.measures:
                expected = _measure(group, key, adapter)
                precision = 1 if adapter.measures[key].agg == "rate" else 2 if adapter.measures[key].field else 0
            elif heading in {"orders", "shipments", "count", "total orders"}:
                expected = len(group)
            elif heading in {"delivered", "cancelled", "rto"}:
                expected = sum(r["status_category"] == heading for r in group)
            elif "delivery rate" in heading:
                expected, precision = _pct(sum(r["status_category"] == "delivered" for r in group), len(group)), 1
            elif "shipping charge" in heading or "average charge" in heading:
                expected = _stats(group, "charge")["avg" if "average" in heading else "sum"]
                precision = 2
            elif heading == "valid charges":
                expected = _stats(group, "charge")["count"]
            else:
                continue
            _check_value(audit, "table_value", cell, expected, precision, f"{reference}.rows[{j}][{k}]")
    if "other" in shown and "other" not in groups:
        others = sum(len(v) for k, v in groups.items() if k not in shown)
        cell = next(v for v in data if str(v[0]).casefold() == "other")
        if len(cell) > 1:
            _check_value(audit, "other_bucket", cell[1], others, reference=reference)
    omitted = len(set(groups) - shown - {"not specified"})
    top = table.get("truncated") or "top" in table.get("title", "").lower()
    if omitted:
        audit.add("table_coverage", True if top or table.get("note") else False,
                  len(groups), len(shown), reference, "omitted categories require declared top-N/truncation")
    if top and data and len(columns) > 1 and columns[1].lower() in {"orders", "count", "total orders"}:
        counts = [number(v[1]) for v in data if str(v[0]).casefold() != "other"]
        expected = sorted((len(v) for v in groups.values()), reverse=True)[:len(counts)]
        audit.add("top_n_order", counts == expected, expected, counts, reference,
                  "ranked counts; omitted groups excluded from displayed total")


def revision_payload(document):
    from app.models.report_schema import ReportDocument
    public = document.model_dump(mode="json") if hasattr(document, "model_dump") else document
    # Exports use ReportDocument rather than ReportResponse; bind their shared public fields.
    return {key: public.get(key) for key in ReportDocument.model_fields if key != "validation_token"}


def revision_digest(value, numeric_format=None):
    """Browsers represent 251.0 and 251 as the same JSON number; preserve all actual values."""
    if numeric_format is None:
        return digest(value)  # Previously issued receipts keep their original verification contract.
    if numeric_format != "json_numbers_v1":
        raise ValueError("Unsupported report numeric format")
    def normalize(item):
        if isinstance(item, dict):
            return {key: normalize(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalize(val) for val in item]
        if isinstance(item, float) and item.is_integer():
            return int(item)
        return item
    return digest(normalize(value))


def seal(document, snapshot):
    payload = {"dataset": snapshot.fingerprint, "request_id": snapshot.request_id,
               "report_id": getattr(document, "report_id", None), "numeric_format": "json_numbers_v1",
               "revision": revision_digest(revision_payload(document), "json_numbers_v1"),
               "response": revision_digest({k: v for k, v in document.model_dump(mode="json").items() if k != "validation_token"}, "json_numbers_v1")}
    body = base64.urlsafe_b64encode(canonical(payload).encode()).decode()
    signature = hmac.new(_SIGNING_KEY, body.encode(), hashlib.sha256).hexdigest()
    document.validation_token = body + "." + signature
    return payload


def verify_revision(document):
    try:
        body, signature = (document.validation_token or "").split(".")
        if not hmac.compare_digest(signature, hmac.new(_SIGNING_KEY, body.encode(), hashlib.sha256).hexdigest()):
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(body))
        if payload["revision"] != revision_digest(revision_payload(document), payload.get("numeric_format")):
            raise ValueError
        if hasattr(document, "report_id"):
            public = {k: v for k, v in document.model_dump(mode="json").items() if k != "validation_token"}
            if document.report_id != payload["report_id"] or revision_digest(public, payload.get("numeric_format")) != payload["response"]:
                raise ValueError
        return payload
    except (ValueError, KeyError, TypeError):
        raise ReportRevisionError("This report revision is unvalidated or has changed. Generate the report again before exporting.") from None


def validate_final(report, snapshot, *, plan=None, adapter=None, normalized_rows=None, analysis_rows=None,
                   block_on_failure=True):
    try:
        return _validate_final(report, snapshot, plan=plan, adapter=adapter,
                               normalized_rows=normalized_rows, analysis_rows=analysis_rows,
                               block_on_failure=block_on_failure)
    except (KeyError, IndexError, TypeError, InvalidOperation, OverflowError):
        audit = Audit(snapshot, report.report_id)
        audit.add("component_structure", False, "valid deterministic component", "unreadable", "response.json",
                  "Report component could not be checked safely")
        audit.finish()


def _validate_final(report, snapshot, *, plan=None, adapter=None, normalized_rows=None, analysis_rows=None,
                    block_on_failure=True):
    public = report.model_dump(mode="json")
    audit = Audit(snapshot, report.report_id)
    from pydantic import ValidationError
    from app.models.report_schema import ReportResponse
    try:
        ReportResponse.model_validate(public)
        audit.add("final_public_schema", True, "valid", "valid", "response.json")
    except ValidationError:
        audit.add("final_public_schema", False, "valid", "invalid", "response.json", "serialized response failed schema validation")
    records = snapshot.records()
    selected_timezone = json.loads(snapshot.filters_json).get("timezone", "Asia/Kolkata")
    reference = [dict(reference_row(r, selected_timezone), _index=i) for i, r in enumerate(records)]
    rows = subset(reference, plan)
    completeness = json.loads(snapshot.completeness_json)
    raw = json.loads(snapshot.raw_json)
    raw_bodies = Counter(digest(record) for record in raw)
    kept_bodies = Counter(digest(record) for record in records)
    added = sum(max(count - raw_bodies[key], 0) for key, count in kept_bodies.items())
    removed = len(raw) - len(records)
    explained = completeness.get("duplicate_records_removed", 0) + completeness.get("conflicting_duplicates", 0)
    explained += completeness.get("unique_records", len(records)) - completeness.get("analyzed_records", len(records))
    audit.add("source_identities", added == 0 and removed == explained, explained, removed,
              "source.raw_to_normalized", "added records forbidden; removals must reconcile with source gate")
    audit.stages = {"api_total": completeness.get("expected_records"), "raw_fetched": len(raw),
                    "normalized_unique": len(records), "analysis_subset": len(rows),
                    "analysis_fingerprint": digest([records[r["_index"]] for r in rows]),
                    "requested_filters": json.loads(snapshot.filters_json)}
    reconciled = len(raw)
    if completeness.get("expected_records") != reconciled:
        reconciled -= completeness.get("duplicate_records_removed", 0)
    audit.add("source_count", None if completeness.get("expected_records") is None or completeness.get("status") == "partial" else
              completeness["expected_records"] == reconciled, completeness.get("expected_records"), reconciled, "source.raw",
              f"raw_fetched={len(raw)} duplicates_removed={completeness.get('duplicate_records_removed', 0)}; source cohort, not account-wide total")
    from app.analysis.completeness import _identity
    identities = [_identity(_flatten(r)) for r in records]
    identified = [key for key in identities if key is not None]
    audit.add("unique_count", None if len(identified) != len(records) else len(identified) == len(set(identified)),
              len(records), len(set(identified)) + len(records) - len(identified), "source.normalized",
              "identity verification unavailable for rows without IDs" if len(identified) != len(records) else "")
    if analysis_rows is not None:
        expected_indexes = sorted(r["_index"] for r in rows)
        actual_indexes = sorted(r["_index"] for r in analysis_rows)
        audit.add("subset_identities", expected_indexes == actual_indexes, digest(expected_indexes), digest(actual_indexes),
                  "analysis_subset.identities", "hashed index multisets from the same immutable dataset")
    upstream = report.structured_report.verification if report.structured_report else None
    for i, check in enumerate((upstream or {}).get("checks") or []):
        if check["status"] == "failed":
            audit.add("builder_validation", False, "passed", "failed", f"builder.checks[{i}]",
                      "existing deterministic validator rejected a component; delivery blocked")
    if normalized_rows is not None:
        # Compare mappings before trusting any production normalization or its subset selection.
        for key in ("status_category", "courier", "destination_city", "destination_state", "pickup_date", "delivery_date", "courier_remarks", "rto_reason"):
            ok = len(normalized_rows) == len(reference) and all(a.get(key) == b.get(key) for a, b in zip(normalized_rows, reference))
            audit.add("normalization_" + key, ok, True, ok, "source.normalized", "independent raw-field mapping")
        for key in ("charge", "order_value"):
            ok = len(normalized_rows) == len(reference) and all(
                (a.get(key) is None and b.get(key) is None) or
                (a.get(key) is not None and b.get(key) is not None and float(a[key]) == float(b[key]))
                for a, b in zip(normalized_rows, reference))
            audit.add("normalization_" + key, ok, True, ok, "source.normalized", "source monetary values before display rounding")
    _check_value(audit, "analysis_subset", public["records_analyzed"], len(rows), reference="records_analyzed")
    m = public.get("metrics") or {}
    if "total_orders" in m:
        _check_value(audit, "metric_count", m["total_orders"], len(rows), reference="metrics.total_orders")
    status = Counter(r["status_category"] for r in rows)
    for key, value in (m.get("rates") or {}).items():
        _check_value(audit, "status_rate_denominator", value, _pct(status[key.removesuffix("_percent")], len(rows)), 1,
                     "metrics.rates;denominator=analysis_subset")
    for field, metric, label_key, count_key in (("courier", "courier_performance", "courier", "total_orders"),
            ("destination_city", "top_destination_cities", "city", "count"),
            ("destination_state", "top_destination_states", "state", "count")):
        counts = Counter(str(r[field]).casefold() for r in rows if r.get(field))
        for i, item in enumerate(m.get(metric) or []):
            label = str(item[label_key]).casefold()
            audit.add("metric_category", label in counts, True, label in counts, f"metrics.{metric}[{i}]", "names withheld")
            _check_value(audit, "metric_group_count", item[count_key], counts[label], reference=f"metrics.{metric}[{i}]")
            if "percent" in item:
                _check_value(audit, "metric_group_percent", item["percent"], _pct(counts[label], len(rows)), 1,
                             f"metrics.{metric}[{i}];denominator=analysis_subset")
    breakdown = m.get("status_category_breakdown")
    if isinstance(breakdown, list):
        categories = [c["category"] for c in breakdown]
        audit.add("status_categories", len(categories) == len(set(categories)) and set(categories) == set(status),
                  len(status), len(set(categories)), "metrics.status_category_breakdown", "unique categories must cover the subset")
        audit.add("status_total", sum(c["count"] for c in breakdown) == len(rows), len(rows), sum(c["count"] for c in breakdown), "metrics.status_category_breakdown")
        for i, item in enumerate(breakdown):
            _check_value(audit, "status_count", item["count"], status[item["category"]], reference=f"metrics.status[{i}]")
            _check_value(audit, "status_percentage", item["percent"], _pct(status[item["category"]], len(rows)), 1, f"metrics.status[{i}];denominator=analysis_subset:{len(rows)}")
    charges, goods = _stats(rows, "charge"), _stats(rows, "order_value")
    financial = m.get("shipping_financial") or {}
    for key, stat in (("total_shipping_charges", "sum"), ("average_charge_per_order", "avg"), ("orders_with_valid_charge", "count")):
        if key in financial:
            _check_value(audit, "financial_" + stat, financial[key], charges[stat], 0 if stat == "count" else 2, "metrics.shipping_financial." + key)
    for dimension, key in (("courier", "courier_charges"), ("payment_mode", "payment_mode_charges")):
        for i, item in enumerate(financial.get(key) or []):
            group = [r for r in rows if _label(dimension, r.get(dimension)).casefold() == str(item[dimension]).casefold()]
            stats = _stats(group, "charge")
            _check_value(audit, "financial_group_count", item["orders"], len(group), reference=f"financial.{key}[{i}]")
            _check_value(audit, "financial_group_sum", item["total_shipping_charges"], stats["sum"], 2, f"financial.{key}[{i}]")
            _check_value(audit, "financial_group_average", item["average_charge_per_order"], stats["avg"], 2, f"financial.{key}[{i}]")
    insights = public.get("insights") or {}
    for key, expected in (("orders", len(rows)), ("spend", charges["sum"]), ("avg_charge", charges["avg"]),
                          ("median_charge", charges["median"]), ("goods_value", goods["sum"])):
        if key in insights.get("kpis", {}):
            _check_value(audit, "insights_" + key, insights["kpis"][key], expected, 0 if key == "orders" else 2, "insights.kpis." + key)
    if m.get("order_value"):
        _check_value(audit, "invoice_sum", m["order_value"].get("total"), goods["sum"], 2, "metrics.order_value.total")
    structured = public.get("structured_report") or {}
    for i, card in enumerate((structured.get("metrics") or []) + (insights.get("kpi_cards") or [])):
        key = card["key"]
        expected = {"orders": len(rows), "total_orders": len(rows), "spend": charges["sum"],
                    "avg_charge": charges["avg"], "median_charge": charges["median"], "goods_value": goods["sum"],
                    "total_shipping_charges": charges["sum"], "average_charge_per_order": charges["avg"],
                    "distinct_couriers": len({r["courier"].casefold() for r in rows if r["courier"]})}.get(key)
        if expected is not None:
            _check_value(audit, "display_metric", card["value"], expected, 2 if key in {"spend", "avg_charge", "median_charge", "goods_value", "total_shipping_charges", "average_charge_per_order"} else 0, f"structured.metrics[{i}]")
        if len(status) == 1 and (plan and plan.scoped):
            audit.add("scope_label", "all statuses" not in card["label"].lower(), True, "all statuses" not in card["label"].lower(), f"structured.metrics[{i}].label")
        if "median" in str(card.get("note") or "").lower():
            import re
            match = re.search(r"(?i)median\s*(?:₹|INR|Rs\.?)?\s*([\d,.]+)", card["note"])
            if match:
                _check_value(audit, "display_median", match[1], charges["median"], 2, f"structured.metrics[{i}].note")
    tables = _tables(public)
    for i, table in enumerate(tables):
        check_table(audit, table, rows, i, adapter, plan)
    _check_charts(audit, structured.get("charts") or [], rows, adapter)
    _coverage(audit, public, tables, rows, plan)
    filters = json.loads(snapshot.filters_json)
    date_check = (completeness.get("filters", {}).get("date") or {})
    if filters.get("from_date") or filters.get("to_date"):
        audit.add("selected_period_preserved", structured.get("scope", {}).get("selected_period") == filters,
                  filters, structured.get("scope", {}).get("selected_period"), "structured.scope.selected_period")
        display_scope = structured.get("scope") or {}
        if display_scope.get("display_period"):
            expected_display = {"from": filters.get("from_date"), "to": filters.get("to_date")}
            actual_display = {key: (display_scope.get("requested_period") or {}).get(key) for key in ("from", "to")}
            audit.add("display_period_preserved", actual_display == expected_display
                      and display_scope["display_period"] == filters, expected_display,
                      actual_display, "structured.scope.display_period")
        if insights.get("scope"):
            actual = {key: insights["scope"].get(key) for key in ("from_date", "to_date")}
            expected = {key: filters.get(key) for key in ("from_date", "to_date")}
            audit.add("insights_period_preserved", actual == expected, expected, actual, "insights.scope")
        audit.add("selected_date_scope", True if date_check.get("verification") == "verified" else None,
                  filters.get("date_field"), date_check.get("verification"), "source.filters.date",
                  "source filter sent; independent row-level verification unavailable" if date_check.get("verification") != "verified" else "requested field and timezone checked by source gate")
    _narrative(audit, public, rows, filters)
    audit.add("narrative_claims", None, reference="content,structured_report",
              explanation="Numeric claims are checked upstream; final natural-language semantics remain UNVERIFIED")
    invalid_schema = any(c["check_id"] == "final_public_schema" and c["status"] == "FAIL" for c in audit.checks)
    event = audit.finish(block=block_on_failure or invalid_schema)
    receipt = seal(report, snapshot)
    logger.info("Report revision sealed: request_id=%s report_id=%s dataset=%s revision=%s response=%s accuracy_failed=%s",
                snapshot.request_id, report.report_id, snapshot.fingerprint, receipt["revision"], receipt["response"], event["failed"])
    return report


def _narrative(audit, public, rows, filters):
    from app.services.report_evidence import validate_numerical_claims
    statuses = Counter(r["status_category"] for r in rows)
    metrics = {"total_orders": len(rows), "distinct_couriers": len({r["courier"].casefold() for r in rows if r["courier"]}),
               "status_category_breakdown": [{"category": k, "count": v, "percent": round(float(_pct(v, len(rows))), 1)} for k, v in statuses.items()],
               "rates": {k + "_percent": round(float(_pct(v, len(rows))), 1) for k, v in statuses.items()}}
    for field, name in (("courier", "courier_performance"), ("destination_city", "top_destination_cities"),
                        ("destination_state", "top_destination_states"), ("payment_mode", "payment_mode_breakdown")):
        counts = Counter(r.get(field) for r in rows if r.get(field))
        metrics[name] = [{field: k, "count": v, "total_orders": v, "percent": round(float(_pct(v, len(rows))), 1)} for k, v in counts.items()]
    charges, values = _stats(rows, "charge"), _stats(rows, "order_value")
    metrics["shipping_financial"] = {"total_shipping_charges": str(charges["sum"]), "average_charge_per_order": str(charges["avg"])}
    metrics["order_value"] = {"total": str(values["sum"]), "average": str(values["avg"])}
    dates = sorted(r["order_date"] for r in rows if r["order_date"])
    metrics["order_date_range"] = {"from": dates[0].isoformat(), "to": dates[-1].isoformat()} if dates else {}
    metrics["scope"] = {"source_records": len(audit.snapshot.records()), "selected_period": filters,
                        "records_analyzed": len(rows)}
    # This check is deliberately conservative: unknown semantic associations are WARN, never PASS.
    for location, narrative in (("content", public.get("content") or {}),
                                ("structured", {k: v for k, v in (public.get("structured_report") or {}).items()
                                                 if k in {"executive_summary", "findings", "recommendations"}})):
        from app.errors import AIResponseError
        try:
            validate_numerical_claims(narrative, metrics, [])
            rejected = []
        except AIResponseError as exc:
            rejected = getattr(exc, "rejections", [])
        import re
        # Explicit observed cohort counts have an unambiguous deterministic denominator.
        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for key, item in value.items():
                    if key in {"executive_summary", "paragraphs", "detail", "text", "content", "sections", "findings"}:
                        yield from strings(item)
            elif isinstance(value, list):
                for item in value:
                    yield from strings(item)
        for text in strings(narrative):
            for match in re.finditer(r"(?<![\w.])(\d[\d,]*)\s+(?:(supplied|fetched|delivered|cancelled|rto)\s+)?(?:shipments|orders)\s+(?:were |are |have been )?(analysed|analyzed|reviewed|fetched|received|delivered|cancelled)\b", text, re.I):
                qualifier = (match[2] or "").lower()
                if match[3].lower() in {"delivered", "cancelled"}:
                    qualifier = match[3].lower()
                expected = len(audit.snapshot.records()) if qualifier in {"supplied", "fetched"} else statuses[qualifier] if qualifier in {"delivered", "cancelled", "rto"} else len(rows)
                _check_value(audit, "narrative_cohort_count", match[1], expected, reference=location + ".observed_count")
        for i, rejection in enumerate(rejected):
            audit.add("narrative_numeric_unverified", None, reference=f"{location}.claim[{i}]",
                      explanation="Final claim not independently bound; upstream numeric validation remains enforced")


def repair_coverage(report, snapshot, plan, adapter):
    """Rebuild absent requested groupings only; incorrect existing numbers are never overwritten."""
    if not plan or not report.structured_report:
        return
    from app.analysis.executor import make_table
    from app.models.report_schema import DisplaySection, TableBlock
    rows = subset([reference_row(r, json.loads(snapshot.filters_json).get("timezone", "Asia/Kolkata")) for r in snapshot.records()], plan)
    present = {_dimension(t) for t in _tables(report.model_dump(mode="json"))}
    additions = []
    for index, spec in enumerate(plan.tables):
        if spec.kind != "grouped" or spec.series or spec.group_by in present:
            continue
        field = spec.group_by
        if field not in {"courier", "destination_city", "destination_state", "rto_reason"}:
            continue
        if not any(r.get(field) for r in rows):
            note = f"Not available: {field.replace('_', ' ')}; the supplied shipments do not include this field."
            if note not in report.structured_report.limitations:
                report.structured_report.limitations.append(note)
            continue
        groups = defaultdict(list)
        for row in rows:
            groups[_label(field, row.get(field))].append(row)
        measures = spec.columns or ("orders",)
        columns = [(field, field.replace("_", " ").title(), "text", None)]
        for key in measures:
            measure = adapter.measures.get(key)
            columns.append((key, measure.label if measure else "Orders", "number",
                            1 if measure and measure.agg == "rate" else 2 if measure and measure.field else 0))
        records = [[label, *[_measure(items, key, adapter) for key in measures]] for label, items in groups.items()]
        records.sort(key=lambda r: (r[1] is None, -(r[1] or 0)))
        limit = min(spec.limit or 200, 200)
        note = f"Showing {limit} of {len(records)} groups; omitted groups are not zero." if len(records) > limit else None
        table = make_table(spec.title, columns, records[:limit], table_id=f"accuracy_grouped_{index}", total_rows=len(records), note=note)
        additions.append(table)
        present.add(field)
    if additions:
        room = max(0, 15 - len(report.tables))
        report.tables.extend(additions[:room])
        report.structured_report.sections.append(DisplaySection(id="requested_coverage", title="Requested breakdowns",
            order=max((s.order for s in report.structured_report.sections), default=0) + 1,
            blocks=[TableBlock(**t.model_dump()) for t in additions]))
    if any(t.kind == "duration" for t in plan.tables) and not any(t.get("id") == "plan_duration" for t in _tables(report.model_dump(mode="json"))):
        valid = [r for r in rows if r["status_category"] == "delivered" and r["order_date"] and r["delivery_date"]
                 and r["delivery_date"] >= r["order_date"]
                 and (not r["pickup_date"] or r["order_date"] <= r["pickup_date"] <= r["delivery_date"])]
        if not valid:
            note = "Delivery timelines unavailable: no delivered shipments have consistent order and delivery dates."
            if note not in report.structured_report.limitations:
                report.structured_report.limitations.append(note)
        else:
            values = sorted((r["delivery_date"] - r["order_date"]).days for r in valid)
            median = (values[(len(values)-1)//2] + values[len(values)//2]) / 2
            table = make_table("Delivery Duration", [("courier", "Courier", "text", None),
                ("delivered_with_valid_dates", "Delivered (valid dates)", "number", 0),
                ("average_days", "Average Days", "number", 1), ("median_days", "Median Days", "number", 1),
                ("max_days", "Max Days", "number", 1)],
                [["All couriers", len(valid), sum(values)/len(values), median, max(values)]], table_id="plan_duration",
                note="Days from order to delivery; consistent delivered dates only.")
            if len(report.tables) < 15:
                report.tables.append(table)
            report.structured_report.sections.append(DisplaySection(id="delivery_timeline", title="Delivery timelines",
                order=max((s.order for s in report.structured_report.sections), default=0) + 1,
                blocks=[TableBlock(**table.model_dump())]))
    if any(t.kind == "duration" for t in plan.tables):
        delivered = [r for r in rows if r["status_category"] == "delivered"]
        missing_pickup = sum(r["pickup_date"] is None for r in delivered)
        missing_delivery = sum(r["delivery_date"] is None for r in delivered)
        if missing_pickup or missing_delivery:
            note = f"Delivery timeline coverage: {missing_pickup} of {len(delivered)} delivered shipments lack pickup dates; {missing_delivery} lack delivery dates."
            if note not in report.structured_report.limitations:
                report.structured_report.limitations.append(note)


def _check_charts(audit, charts, rows, adapter):
    for i, chart in enumerate(charts):
        field = (chart.get("x_axis") or {}).get("field")
        measure = (chart.get("y_axis") or {}).get("field")
        series = (chart.get("options") or {}).get("series_field")
        if measure and adapter and measure in adapter.fields and measure not in adapter.measures:
            series = measure
            measure = (chart.get("options") or {}).get("measure")
        if not field:
            field = {"status": "status_category", "courier": "courier", "destination_city": "destination_city",
                     "destination_state": "destination_state"}.get(chart.get("id"))
        if field not in {"courier", "status_category", "destination_city", "destination_state", "payment_mode"}:
            audit.add("chart_unverified", None, reference=f"charts[{i}]", explanation="Unsupported chart form; not claimed verified")
            continue
        groups = defaultdict(list)
        filtered = [r for r in rows if all(r.get(k) == v for k, v in chart.get("options", {}).get("applied_filters", []))]
        for row in filtered:
            label = adapter.label_of(field, row.get(field)) if adapter else _label(field, row.get(field))
            groups[label.casefold()].append(row)
        shown = {str(label).casefold() for label in chart["labels"]}
        audit.add("chart_unique_categories", len(shown) == len(chart["labels"]), len(chart["labels"]), len(shown),
                  f"charts[{i}].labels", "labels withheld")
        if len(chart["datasets"]) > 1 and not series:
            audit.add("chart_multiseries_unverified", None, reference=f"charts[{i}]",
                      explanation="No series mapping; not claimed verified")
            continue
        for dataset in chart["datasets"]:
            for j, (label, value) in enumerate(zip(chart["labels"], dataset["data"])):
                group = groups.get(str(label).casefold(), [])
                base_count = len(group)
                if series and adapter:
                    group = [r for r in group if adapter.label_of(series, r.get(series)) == dataset["label"]]
                if str(label).casefold() == "other" and "other" not in groups:
                    group = [r for k, items in groups.items() if k not in shown for r in items]
                expected = _measure(group, measure or "orders", adapter)
                if series == "status_category" and chart.get("value_format", {}).get("type") == "percent":
                    expected = _pct(len(group), base_count)
                _check_value(audit, "chart_value", value, expected, 2 if measure and measure not in {"orders", "total_orders"} else 0, f"charts[{i}].data[{j}]")


def _coverage(audit, public, tables, rows, plan):
    if not plan:
        return
    present = {_dimension(t) for t in tables}
    structured = public.get("structured_report") or {}
    unavailable = list(structured.get("limitations") or []) + list((public.get("content") or {}).get("data_limitations") or [])
    if plan.wants_charts:
        audit.add("requested_charts", True if structured.get("charts") else None, "present or unavailable",
                  "present" if structured.get("charts") else "unavailable" if structured.get("chart_notes") else "missing",
                  "plan.wants_charts")
    for spec in plan.tables:
        field = spec.group_by
        if spec.kind == "grouped" and field in {"destination_city", "destination_state", "courier", "rto_reason"}:
            exists = field in present
            explicitly_unavailable = any(field.replace("destination_", "").replace("_", " ") in str(note).lower()
                                         and any(w in str(note).lower() for w in ("unavailable", "missing", "not supplied", "not include")) for note in unavailable)
            audit.add("prompt_coverage_" + field, True if exists else None, "present or unavailable", "present" if exists else "unavailable" if explicitly_unavailable else "missing", "plan.tables",
                      "missing component requires repair" if not exists and not explicitly_unavailable else "")
    for spec in plan.tables:
        if spec.kind != "duration":
            continue
        has_duration = any(t.get("id") == "plan_duration" for t in tables)
        valid, missing = [], 0
        for row in rows:
            if row["status_category"] != "delivered":
                continue
            start, end, pickup = row["order_date"], row["delivery_date"], row["pickup_date"]
            if start and end and end >= start and (not pickup or start <= pickup <= end):
                valid.append((end - start).days)
            else:
                missing += 1
        audit.add("delivery_timeline_coverage", True if has_duration else None, len(valid), len(valid) if has_duration else 0,
                  "plan.duration", f"missing_or_inconsistent={missing}; denominator=delivered_subset")
        for t in tables:
            if t.get("id") == "plan_duration" and t.get("rows") and valid:
                for row_index, values in enumerate(t.get("values") or t["rows"]):
                    matched = rows if values[0] == "All couriers" else [r for r in rows if _label("courier", r.get("courier")).casefold() == str(values[0]).casefold()]
                    ordered = sorted((r["delivery_date"]-r["order_date"]).days for r in matched
                        if r["status_category"] == "delivered" and r["order_date"] and r["delivery_date"]
                        and r["delivery_date"] >= r["order_date"] and (not r["pickup_date"] or r["order_date"] <= r["pickup_date"] <= r["delivery_date"]))
                    if not ordered:
                        audit.add("delivery_duration_category", False, "valid delivered dates", 0, f"plan_duration[{row_index}]")
                        continue
                    median = Decimal(ordered[len(ordered)//2]) if len(ordered)%2 else Decimal(ordered[len(ordered)//2-1]+ordered[len(ordered)//2])/2
                    for i, expected in enumerate((len(ordered), Decimal(sum(ordered))/len(ordered), median, max(ordered)), 1):
                        _check_value(audit, "delivery_duration", values[i], expected, 0 if i == 1 else 1, f"plan_duration[{row_index}]")
