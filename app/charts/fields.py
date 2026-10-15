"""Approved chart fields and measures per report type (the only vocabulary the planner or AI may use).

A ``DatasetAdapter`` turns the request data into flat, typed, PII-free rows and declares which
fields can be grouped or measured. New report types get charts by registering an adapter here;
the chart skills and the report service do not change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Literal

FieldKind = Literal["category", "date", "number"]
ValueFormat = Literal["number", "currency", "percent"]


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: FieldKind
    label: str
    # Regex fragments that refer to this field in a user prompt.
    aliases: tuple[str, ...] = ()
    unit: str | None = None
    value_format: ValueFormat = "number"


@dataclass(frozen=True)
class MeasureSpec:
    """A predefined aggregation. Nothing else can be computed; no expressions are evaluated."""
    name: str
    label: str
    agg: Literal["count", "sum", "avg", "rate"]
    field: str | None = None                      # numeric field for sum / avg
    where: tuple[tuple[str, frozenset], ...] = ()  # count filter, or rate numerator
    value_format: ValueFormat = "number"
    aliases: tuple[str, ...] = ()
    additive: bool = True                          # can be summed across groups (pie, treemap, "Other")


@dataclass(frozen=True)
class Stage:
    label: str
    where: tuple[tuple[str, frozenset], ...] = ()
    exclude: tuple[tuple[str, frozenset], ...] = ()


@dataclass(frozen=True)
class DatasetAdapter:
    report_type: str
    record_label: str                              # "Orders", "Remittances"
    fields: dict[str, FieldSpec]
    measures: dict[str, MeasureSpec]
    rows: Callable[[dict[str, Any]], list[dict[str, Any]]]
    default_measure: str
    date_field: str | None = None
    status_field: str | None = None
    # Per chart type default dimension, e.g. {"pie": "status_category", "bar": "courier"}.
    default_dimension: dict[str, str] = field(default_factory=dict)
    numeric_defaults: tuple[str, ...] = ()         # histogram / box plot / scatter preference order
    funnel: tuple[Stage, ...] = ()
    waterfall_total: str | None = None             # waterfall: total, minus each status, equals the last status
    waterfall_steps: tuple[str, ...] = ()
    rate_measures: tuple[str, ...] = ()            # radar axes / gauge candidates
    geo_field: str | None = None
    category_labels: dict[str, dict[str, str]] = field(default_factory=dict)
    filter_values: dict[str, dict[str, str]] = field(default_factory=dict)  # field -> {regex: value}

    def label_of(self, field_name: str, value: Any) -> str:
        if value is None or value == "":
            return "Not specified"
        if isinstance(value, date):
            return value.isoformat()
        return self.category_labels.get(field_name, {}).get(str(value), str(value))


def _is(*values: str) -> frozenset:
    return frozenset(values)


# --- Shipments ---------------------------------------------------------------

_SHIPPED = _is("in_transit", "delivered", "rto", "exception")


def _shipment_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    from app.prompts.shipment_prompt import extract_orders, parse_order
    rows = []
    for record in extract_orders(data):
        order = parse_order(record)
        rows.append({
            "status_category": order.category,
            "status": order.status,
            "courier": order.courier,
            "payment_mode": order.payment_mode,
            "cancellation_reason": order.cancellation_reason,
            "destination_state": order.destination_state,
            "destination_city": order.destination_city,
            "zone": order.zone,
            "order_date": order.order_date,
            "weekday": order.order_date.strftime("%A") if order.order_date else None,
            "order_value": order.order_value,
            "weight": order.weight,
            "charge": order.charge,
        })
    return rows


SHIPMENT_ADAPTER = DatasetAdapter(
    report_type="shipment",
    record_label="Orders",
    fields={f.name: f for f in (
        FieldSpec("courier", "category", "Courier", (r"courier", r"carrier", r"partner")),
        FieldSpec("status_category", "category", "Shipment Status", (r"status", r"outcome", r"stage")),
        FieldSpec("payment_mode", "category", "Payment Mode", (r"payment", r"\bcod\b", r"prepaid", r"to\s?pay")),
        FieldSpec("destination_state", "category", "Destination State", (r"\bstates?\b", r"state[- ]wise", r"region", r"geograph", r"destination")),
        FieldSpec("destination_city", "category", "Destination City", (r"\bcity\b", r"\bcities\b", r"city[- ]wise")),
        FieldSpec("zone", "category", "Zone", (r"\bzones?\b",)),
        FieldSpec("cancellation_reason", "category", "Cancellation Reason", (r"reasons?",)),
        FieldSpec("weekday", "category", "Weekday", (r"weekdays?", r"day of (?:the )?week", r"day[- ]wise pattern")),
        FieldSpec("order_date", "date", "Order Date", (r"\bdates?\b", r"daily", r"\bday\b", r"day[- ]wise", r"weekly", r"monthly",
                                                      r"trends?", r"over time", r"timeline", r"time series")),
        FieldSpec("order_value", "number", "Order Value", (r"order values?", r"revenue", r"\bgmv\b", r"sales", r"invoice"),
                  value_format="currency"),
        FieldSpec("weight", "number", "Weight", (r"weights?", r"\bkg\b"), unit="kg"),
        FieldSpec("charge", "number", "Shipping Charge", (r"charges?", r"freight", r"shipping cost", r"\bcost\b"),
                  value_format="currency"),
    )},
    measures={m.name: m for m in (
        # Most specific first: the planner takes the first measure whose alias matches.
        MeasureSpec("delivery_rate", "Delivery Rate", "rate", where=(("status_category", _is("delivered")),),
                    value_format="percent", additive=False,
                    aliases=(r"delivery (?:rate|performance|success)", r"success rate", r"delivered (?:rate|percent)", r"performance")),
        MeasureSpec("cancellation_rate", "Cancellation Rate", "rate", where=(("status_category", _is("cancelled")),),
                    value_format="percent", additive=False, aliases=(r"cancell?ation rate", r"cancel(?:led)? (?:rate|percent)")),
        MeasureSpec("rto_rate", "RTO Rate", "rate", where=(("status_category", _is("rto")),),
                    value_format="percent", additive=False, aliases=(r"rto rate", r"return rate")),
        MeasureSpec("exception_rate", "Exception Rate", "rate", where=(("status_category", _is("exception")),),
                    value_format="percent", additive=False, aliases=(r"exception rate", r"ndr rate", r"failure rate")),
        MeasureSpec("avg_charge", "Average Shipping Charge", "avg", field="charge", value_format="currency", additive=False,
                    aliases=(r"(?:average|avg|mean) (?:shipping )?(?:charge|freight|cost)",)),
        MeasureSpec("avg_order_value", "Average Order Value", "avg", field="order_value", value_format="currency", additive=False,
                    aliases=(r"(?:average|avg|mean) order value", r"\baov\b")),
        MeasureSpec("avg_weight", "Average Weight (kg)", "avg", field="weight", additive=False,
                    aliases=(r"(?:average|avg|mean) weight",)),
        MeasureSpec("shipping_charges", "Shipping Charges", "sum", field="charge", value_format="currency",
                    aliases=(r"charges?", r"freight", r"shipping cost", r"\bcost\b", r"spend")),
        MeasureSpec("order_value", "Order Value", "sum", field="order_value", value_format="currency",
                    aliases=(r"order values?", r"revenue", r"\bgmv\b", r"sales")),
        MeasureSpec("weight", "Total Weight (kg)", "sum", field="weight", aliases=(r"total weight", r"weights?")),
        MeasureSpec("cancelled_orders", "Cancelled Orders", "count", where=(("status_category", _is("cancelled")),),
                    aliases=(r"cancel",)),
        MeasureSpec("rto_orders", "RTO Orders", "count", where=(("status_category", _is("rto")),), aliases=(r"\brto\b", r"returns?")),
        MeasureSpec("exception_orders", "Exception Orders", "count", where=(("status_category", _is("exception")),),
                    aliases=(r"exceptions?", r"\bndr\b", r"undeliver", r"failed")),
        MeasureSpec("delivered_orders", "Delivered Orders", "count", where=(("status_category", _is("delivered")),),
                    aliases=(r"deliver(?:ed|ies)",)),
        MeasureSpec("in_transit_orders", "In-Transit Orders", "count", where=(("status_category", _is("in_transit")),),
                    aliases=(r"in[- ]transit",)),
        MeasureSpec("pending_orders", "Pending Orders", "count", where=(("status_category", _is("pending")),),
                    aliases=(r"pending",)),
        MeasureSpec("orders", "Orders", "count", aliases=(r"shipments?", r"orders?", r"volume", r"count", r"distribution")),
    )},
    rows=_shipment_rows,
    default_measure="orders",
    date_field="order_date",
    status_field="status_category",
    default_dimension={"pie": "status_category", "doughnut": "status_category", "treemap": "courier",
                       "bar": "courier", "horizontal_bar": "courier", "grouped_bar": "courier", "stacked_bar": "courier",
                       "line": "order_date", "area": "order_date", "heatmap": "courier", "boxplot": "courier",
                       "bubble": "courier", "radar": "courier", "map": "destination_state"},
    numeric_defaults=("charge", "order_value", "weight"),
    funnel=(Stage("Orders Booked"),
            Stage("Not Cancelled", exclude=(("status_category", _is("cancelled")),)),
            Stage("Shipped", where=(("status_category", _SHIPPED),)),
            Stage("Delivered", where=(("status_category", _is("delivered")),))),
    waterfall_total="Total Orders",
    waterfall_steps=("cancelled", "pending", "in_transit", "rto", "exception", "other", "unknown", "delivered"),
    rate_measures=("delivery_rate", "cancellation_rate", "rto_rate", "exception_rate"),
    geo_field="destination_state",
    category_labels={"status_category": {"delivered": "Delivered", "cancelled": "Cancelled", "in_transit": "In Transit",
                                         "pending": "Pending", "rto": "RTO", "exception": "Exception / NDR",
                                         "other": "Other", "unknown": "Unknown"}},
    filter_values={"status_category": {r"\bdelivered\b": "delivered", r"cancell?ed": "cancelled", r"\brto\b": "rto",
                                       r"in[- ]transit": "in_transit", r"\bpending\b": "pending", r"\bndr\b|exception": "exception"},
                   "payment_mode": {r"\bcod\b": "COD", r"prepaid": "Prepaid", r"to\s?pay": "ToPay"}},
)


# --- Remittances -------------------------------------------------------------

def _remittance_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    from app.prompts.remittance_prompt import (ACCEPTED_PAID_PATHS, ACCEPTED_PENDING_PATHS, _extract_list, _to_decimal)
    from app.prompts.shipment_prompt import parse_date
    records = _extract_list(data, ACCEPTED_PAID_PATHS) or _extract_list(data, ACCEPTED_PENDING_PATHS)

    def number(*values):
        for value in values:
            parsed = _to_decimal(value)
            if parsed is not None:
                return float(parsed)
        return None

    return [{
        "status": str(r.get("transactionStatus") or r.get("status") or "").strip() or None,
        "remittance_date": parse_date(r.get("remittanceDate") or r.get("paidDate") or r.get("dueDate") or r.get("date")
                                      or r.get("createdAt")),
        "remitted_amount": number(r.get("remittedAmount"), r.get("amount"), r.get("remittanceDueAmount")),
        "deduction": number(r.get("codDeductionAmount"), r.get("deduction")),
        "collectable": number(r.get("collectableValue"), r.get("collectableAmount")),
    } for r in records if isinstance(r, dict)]


REMITTANCE_ADAPTER = DatasetAdapter(
    report_type="remittance",
    record_label="Remittances",
    fields={f.name: f for f in (
        FieldSpec("status", "category", "Transaction Status", (r"status",)),
        FieldSpec("remittance_date", "date", "Remittance Date", (r"\bdates?\b", r"daily", r"weekly", r"monthly", r"trends?",
                                                                 r"over time", r"timeline")),
        FieldSpec("remitted_amount", "number", "Remitted Amount", (r"remitt?ed", r"amount", r"payouts?"), value_format="currency"),
        FieldSpec("deduction", "number", "COD Deduction", (r"deductions?",), value_format="currency"),
        FieldSpec("collectable", "number", "Collectable Value", (r"collectable",), value_format="currency"),
    )},
    measures={m.name: m for m in (
        MeasureSpec("deduction", "COD Deductions", "sum", field="deduction", value_format="currency", aliases=(r"deductions?",)),
        MeasureSpec("collectable", "Collectable Value", "sum", field="collectable", value_format="currency", aliases=(r"collectable",)),
        MeasureSpec("remitted_amount", "Remitted Amount", "sum", field="remitted_amount", value_format="currency",
                    aliases=(r"remitt?ed", r"amount", r"payouts?", r"value")),
        MeasureSpec("remittances", "Remittances", "count", aliases=(r"remittances?", r"transactions?", r"records?", r"count",
                                                                   r"distribution")),
    )},
    rows=_remittance_rows,
    default_measure="remittances",
    date_field="remittance_date",
    status_field="status",
    default_dimension={"pie": "status", "doughnut": "status", "treemap": "status", "bar": "status", "horizontal_bar": "status",
                       "line": "remittance_date", "area": "remittance_date", "boxplot": "status"},
    numeric_defaults=("remitted_amount", "deduction", "collectable"),
)


ADAPTERS: dict[str, DatasetAdapter] = {
    "shipment": SHIPMENT_ADAPTER,
    "paid_remittance": REMITTANCE_ADAPTER,
    "pending_remittance": REMITTANCE_ADAPTER,
}


def register_adapter(report_type: str, adapter: DatasetAdapter) -> None:
    ADAPTERS[report_type] = adapter


def get_adapter(report_type: str) -> DatasetAdapter | None:
    return ADAPTERS.get(report_type)
