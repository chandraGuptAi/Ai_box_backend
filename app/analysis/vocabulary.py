"""Per report type: the only fields, filters, columns and wording an analysis plan may use.

A new reporting domain gets prompt-driven reports by registering a ``PlanVocabulary`` (and the
chart ``DatasetAdapter`` it builds on); the interpreter, executor and verifier do not change.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from app.charts.fields import DatasetAdapter


@dataclass(frozen=True)
class ColumnSpec:
    label: str
    type: str = "text"               # text | number | currency | percent | date
    decimals: int = 0


@dataclass(frozen=True)
class DataContext:
    """Facts about the current request's payload (never about earlier requests)."""
    records_supplied: int
    as_of: date | None = None        # the payload's own reference date, if supplied
    api_total: int | None = None     # e.g. getOrderList.total on a paginated response
    account_total: int | None = None  # account-wide counter, e.g. account_summary.all_orders
    api_filter: dict[str, str] = field(default_factory=dict)
    api_period: tuple[date, date] | None = None
    fingerprint: str = ""


@dataclass(frozen=True)
class PlanVocabulary:
    report_type: str
    record_noun: str                                  # "shipment"
    record_pattern: str                               # regex for record nouns in a prompt
    status_values: tuple[str, ...]
    status_labels: dict[str, str]
    status_words: tuple[tuple[str, str], ...]         # (regex, status value)
    status_nouns: tuple[tuple[str, str], ...]         # ("cancellations", "cancelled")
    detail_columns: dict[str, ColumnSpec]
    default_detail_columns: tuple[str, ...]
    column_words: tuple[tuple[str, str], ...]         # (regex, detail column)
    numeric_filter_words: tuple[tuple[str, str], ...]  # (regex, numeric field)
    name_filter_fields: tuple[str, ...]               # matched against values present in the data
    name_stopwords: frozenset[str]
    payment_field: str | None
    payment_words: tuple[tuple[str, str], ...]
    unavailable: tuple[tuple[str, str, str], ...]     # (regex, requested item, why it cannot be answered)
    interpretations: tuple[tuple[str, str], ...]      # (regex, how the wording is interpreted)
    required_fields: tuple[tuple[str, str], ...]      # (regex, field the wording needs)
    topic_patterns: tuple[tuple[str, str], ...]
    filter_topics: dict[str, str]                     # field or "field=value" -> report topic
    # Dimension -> measures its standard table already shows; a grouping asking for more gets its own table.
    standard_dimensions: dict[str, frozenset[str]]
    duration_pattern: str | None
    context: Callable[[dict[str, Any]], DataContext]
    status_field: str | None = None
    date_field: str | None = None
    id_column: str | None = None
    max_table_rows: int = 200
    max_detail_columns: int = 8
    # Prose names of fields (regex -> field); a narrative may not call them unavailable when present.
    absence_subjects: dict[str, str] = field(default_factory=dict)
    # (payload, kept record indexes) -> the same payload holding only those records.
    subset: Callable[[dict[str, Any], set[int]], dict[str, Any]] | None = None

    def label(self, name: str, adapter: DatasetAdapter) -> str:
        if name in self.detail_columns:
            return self.detail_columns[name].label
        spec = adapter.fields.get(name)
        return spec.label if spec else name.replace("_", " ").title()

    def column_type(self, name: str, adapter: DatasetAdapter) -> str:
        if name in self.detail_columns:
            return self.detail_columns[name].type
        spec = adapter.fields.get(name)
        if spec is None:
            return "text"
        return {"number": "currency" if spec.value_format == "currency" else "number", "date": "date"}.get(spec.kind, "text")

    def is_numeric(self, name: str, adapter: DatasetAdapter) -> bool:
        return self.column_type(name, adapter) in ("number", "currency", "percent")

    def filterable(self, name: str, adapter: DatasetAdapter) -> bool:
        return name in adapter.fields and adapter.fields[name].kind != "date"


VOCABULARIES: dict[str, PlanVocabulary] = {}


def register_vocabulary(vocab: PlanVocabulary, *report_types: str) -> None:
    for key in report_types or (vocab.report_type,):
        VOCABULARIES[key] = vocab


def get_vocabulary(report_type: str) -> PlanVocabulary | None:
    return VOCABULARIES.get(report_type)


def fingerprint(records: list[Any]) -> str:
    """Stable digest of the supplied records; identifies which data a report was built from."""
    digest = hashlib.sha256(json.dumps(records, sort_keys=True, default=str, ensure_ascii=True).encode("utf-8"))
    return digest.hexdigest()[:20]


# --- Shipments ------------------------------------------------------------------

def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _shipment_context(data: dict[str, Any]) -> DataContext:
    from app.prompts.shipment_prompt import _orders_path, extract_orders, parse_date, scrub_text
    records = extract_orders(data)
    container = data.get("data") if isinstance(data.get("data"), dict) else data
    path = _orders_path(data) or ()
    parent = data
    for key in path[:-1]:
        parent = parent[key]
    # A paginated list ({"data": [...], "total": N}) carries the API's own total next to the page.
    api_total = None
    if isinstance(parent, dict) and parent is not container and parent is not data:
        api_total = next((_int(parent.get(k)) for k in ("total", "totalRecords", "total_records", "totalCount",
                                                         "total_count", "recordsTotal") if _int(parent.get(k)) is not None), None)
    summary = container.get("account_summary") if isinstance(container.get("account_summary"), dict) else \
        data.get("account_summary") if isinstance(data.get("account_summary"), dict) else {}
    raw_filter = container.get("report_filter") if isinstance(container.get("report_filter"), dict) else {}
    api_filter = {k: scrub_text(raw_filter.get(k), 60) for k in ("status_label", "status", "courier", "payment_mode",
                                                                 "date_field")}
    # Dates are parsed, not scrubbed: the phone-number scrubber would redact "2026-08-14".
    start, end = parse_date(raw_filter.get("from_date")), parse_date(raw_filter.get("to_date"))
    api_filter.update({"from_date": start.isoformat() if start else None, "to_date": end.isoformat() if end else None})
    api_filter = {k: v for k, v in api_filter.items() if v}
    if api_filter.get("date_field", "order_date") != "order_date":
        start = end = None  # the period is of another date field; order dates are not compared with it
    as_of = parse_date(container.get("as_of")) or parse_date(data.get("as_of"))
    return DataContext(records_supplied=len(records), as_of=as_of, api_total=api_total,
                       account_total=_int(summary.get("all_orders")), api_filter=api_filter,
                       api_period=(start, end) if start and end and start <= end else None,
                       fingerprint=fingerprint(records))


def _shipment_subset(data: dict[str, Any], keep: set[int]) -> dict[str, Any]:
    from app.prompts.shipment_prompt import extract_orders, replace_orders
    return replace_orders(data, [record for index, record in enumerate(extract_orders(data)) if index in keep])


_MONEY = ColumnSpec
SHIPMENT_VOCABULARY = PlanVocabulary(
    report_type="shipment",
    record_noun="shipment",
    record_pattern=r"(?:shipments?|orders?|consignments?|parcels?|packages?|records?|awbs?)",
    status_values=("delivered", "in_transit", "pending", "cancelled", "rto", "exception", "lost", "other", "unknown"),
    status_labels={"delivered": "Delivered", "in_transit": "In Transit", "pending": "Pending", "cancelled": "Cancelled",
                   "rto": "RTO", "exception": "Exception / NDR", "lost": "Lost", "other": "Other", "unknown": "Status Missing"},
    # Statuses are matched from the user's words; record statuses use the explicit backend mapping only.
    status_words=((r"\bdelivered\b", "delivered"), (r"\bcancell?ed\b", "cancelled"),
                  (r"\brto\b|\breturned\b|\breturn(?:ed)? to origin\b", "rto"),
                  (r"\bin[- ]transit\b|\bout for delivery\b", "in_transit"),
                  (r"\bpending\b|\bnot picked(?: up)?\b|\bunshipped\b|\bpickup scheduled\b", "pending"),
                  (r"\bundelivered\b|\bndr\b|\bfailed deliver\w*|\bdamaged\b", "exception"), (r"\blost\b", "lost")),
    status_nouns=((r"cancellations", "cancelled"), (r"deliveries", "delivered"), (r"rtos|returns", "rto"),
                  (r"exceptions|ndrs", "exception")),
    detail_columns={
        "order_id": ColumnSpec("Order ID"), "awb_number": ColumnSpec("AWB"), "order_date": ColumnSpec("Order Date", "date"),
        "status": ColumnSpec("Status"), "courier": ColumnSpec("Courier"), "payment_mode": ColumnSpec("Payment Mode"),
        "cancellation_reason": ColumnSpec("Cancellation Reason"), "destination_state": ColumnSpec("Destination State"),
        "rto_reason": ColumnSpec("RTO Reason"),
        "destination_city": ColumnSpec("Destination City"),
        "destination_pincode": ColumnSpec("Destination Pincode"), "zone": ColumnSpec("Zone"),
        "charge": _MONEY("Shipping Charge", "currency", 2), "order_value": _MONEY("Invoice Value", "currency", 2),
        "collectable_amount": _MONEY("Collectable Amount", "currency", 2), "weight": ColumnSpec("Weight (kg)", "number", 2),
        "pickup_date": ColumnSpec("Pickup Date", "date"), "delivery_date": ColumnSpec("Delivery Date", "date"),
    },
    default_detail_columns=("order_id", "order_date", "status", "courier", "payment_mode"),
    column_words=((r"\bpin[- ]?codes?\b|\bpostal codes?\b", "destination_pincode"),
                  (r"\breasons?\b", "cancellation_reason"),
                  (r"\b(?:shipping |total )?charges?\b|\bfreight\b|\bshipping costs?\b", "charge"),
                  (r"\binvoice\b|\border values?\b", "order_value"), (r"\bcollectable\b|\bcod amounts?\b", "collectable_amount"),
                  (r"\bweights?\b", "weight"), (r"\bstates?\b|\bdestinations?\b", "destination_state"),
                  (r"\bcit(?:y|ies)\b", "destination_city"), (r"\bzones?\b", "zone"), (r"\bawbs?\b|\btracking\b", "awb_number"),
                  (r"\bdelivery dates?\b|\bdelivered on\b", "delivery_date"), (r"\bpickup dates?\b", "pickup_date")),
    numeric_filter_words=((r"shipping charges?|shipping costs?|freight(?: charges?)?|total charges?|charges?|costs?", "charge"),
                          (r"invoice(?: total)?(?: values?| amounts?)?|order values?", "order_value"),
                          (r"collectable(?: amounts?| values?)?|cod amounts?", "collectable_amount"),
                          (r"weights?", "weight")),
    name_filter_fields=("courier", "destination_state", "destination_city"),
    name_stopwords=frozenset({"express", "logistics", "courier", "couriers", "surface", "cargo", "prime", "service",
                              "services", "india", "delivery", "deliveries", "freight", "parcel", "shipping", "standard",
                              "priority", "economy", "heavy", "light", "partner", "solutions", "supply", "chain",
                              "transport", "global", "international", "domestic", "smart", "local"}),
    payment_field="payment_mode",
    payment_words=((r"\bcod\b|\bcash on delivery\b|\bcollected at (?:the )?door\b", "COD"), (r"\bprepaid\b", "Prepaid"), (r"\bto[- ]?pay\b", "ToPay")),
    unavailable=(
        (r"\bprofits?\b|\bprofitability\b|\bmargins?\b|\bnet (?:income|earnings?)\b|\bp\s?&\s?l\b|\bearnings\b",
         "profit or margin", "Profit and margin are not in the order data. Shipping charges, invoice values and collectable "
         "amounts are supplied as separate measures, and none of them is profit."),
        (r"\b(?:collected|realis\w+|realiz\w+|received) (?:revenue|amounts?|cash|payments?)\b|\brevenue (?:collected|received|"
         r"realis\w+|realiz\w+)\b|\bremittances?\b|\bpayouts?\b",
         "collected revenue or remittances", "Collected revenue and remittances are not in the order list. The collectable "
         "amount is what is due on COD orders, not what was collected; use the remittance reports for payouts."),
        (r"\bcustomer (?:ratings?|satisfaction|feedback|reviews?)\b|\bnps\b|\bcsat\b",
         "customer satisfaction", "Customer ratings or feedback are not in the order data."),
        (r"\blosses\b|\bloss\b|\blost revenue\b",
         "financial losses", "Financial losses are not recorded. Charges on cancelled orders are recorded charges, not "
         "confirmed losses, and are reported only as such."),
    ),
    interpretations=((r"(?<!collected )\brevenue\b(?! (?:collected|received|realis|realiz))",
                      "Revenue is shown as invoice value as supplied; it is not collected revenue."),),
    required_fields=((r"\bpin[- ]?codes?\b|\bpostal codes?\b", "destination_pincode"),
                     (r"\b(?:rto|return) reasons?\b", "rto_reason"),
                     (r"\bzones?\b", "zone"), (r"\bweights?\b", "weight"), (r"\bcit(?:y|ies)\b", "destination_city"),
                     (r"\bstates?\b|\bstate[- ]wise\b|\bgeograph\w*", "destination_state"),
                     (r"\bpayment\b|\bcod\b|\bprepaid\b", "payment_mode"), (r"\bcouriers?\b|\bcarriers?\b", "courier"),
                     (r"\breasons?\b", "cancellation_reason"), (r"\binvoice\b|\border values?\b|\bgmv\b", "order_value"),
                     (r"\bcharges?\b|\bfreight\b|\bshipping costs?\b", "charge"), (r"\bcollectable\b", "collectable_amount"),
                     (r"\btrends?\b|\bdaily\b|\bweekly\b|\bmonthly\b|\bover time\b", "order_date")),
    topic_patterns=(("financial", r"financial|shipping charg|shipping cost|freight|charges?\b|invoice|collectable"),
                    ("courier", r"courier|carrier"), ("payment", r"payment|\bcod\b|prepaid|to[- ]?pay"),
                    ("cancellation", r"cancel"), ("geography", r"geograph|destination|\bstates?\b|\bcities\b|\bcity\b"),
                    ("exception", r"exception|\bndr\b|undeliver|\blost\b|damage"),
                    ("status", r"status|deliver|\brto\b|in[- ]transit|pending|performance|overall"),
                    ("general", r"report|analy[sz]|summary|overview|dashboard|kpi|insight|shipments?|orders?")),
    filter_topics={"status_category=cancelled": "cancellation", "status_category=exception": "exception",
                   "status_category=rto": "exception", "status_category=lost": "exception", "charge": "financial", "order_value": "financial",
                   "collectable_amount": "financial", "courier": "courier", "payment_mode": "payment",
                   "destination_state": "geography", "destination_city": "geography", "destination_pincode": "geography"},
    standard_dimensions={
        "courier": frozenset({"orders", "delivered_orders", "cancelled_orders", "rto_orders", "delivery_rate",
                              "shipping_charges", "avg_charge"}),
        "payment_mode": frozenset({"orders", "shipping_charges", "avg_charge"}),
        "status_category": frozenset({"orders"}), "destination_state": frozenset({"orders"}),
        "cancellation_reason": frozenset({"orders"})},
    duration_pattern=r"\bdelivery (?:time|duration|speed|days|tat|timelines?)\b|\bturn[- ]?around\b|\btat\b|\btransit time\b|"
                     r"\bdays to deliver\b|\btime to deliver\b|\blead time\b",
    context=_shipment_context,
    status_field="status_category",
    date_field="order_date",
    id_column="order_id",
    absence_subjects={r"cancell?ation reasons?|reasons? for cancell?ation|cancel reasons?|reasons?": "cancellation_reason",
                      r"courier (?:names?|details|data|information)": "courier",
                      r"payment (?:modes?|types?|methods?|data|information)|prepaid|cod": "payment_mode",
                      r"cit(?:y|ies)(?:[- ](?:level|wise))?|destination cit(?:y|ies)": "destination_city",
                      r"(?:shipment |delivery )?status(?:es)?|deliver(?:y|ed|ies)(?! dates?)": "status_category",
                      r"(?:order |booking )?dates?": "order_date",
                      r"(?:shipping |freight )?charges?|freight": "charge",
                      r"destination (?:states?|data)|states?": "destination_state",
                      r"weights?": "weight", r"invoice (?:values?|amounts?)|order values?": "order_value",
                      r"collectable amounts?": "collectable_amount"},
    subset=_shipment_subset,
)

register_vocabulary(SHIPMENT_VOCABULARY, "shipment")
