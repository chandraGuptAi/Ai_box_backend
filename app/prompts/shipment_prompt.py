"""Shipment Analysis Report: order extraction, sanitisation, metrics and prompt.

Only an allow-list of operational fields is ever read from an order. Customer
names, phone numbers, emails, addresses and pincodes are never extracted, and
free-text values are additionally scrubbed of anything that looks like an
email address or phone number before they can reach the AI model.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from app.config import settings
from app.errors import InvalidReportDataError
from app.models.report_schema import KeyMetric, ReportTable
from app.prompts.base import AnalysisResult, ReportDefinition
from app.services.report_evidence import derived_percentages
from app.prompts.shipment_financial import shipping_financial_metrics, financial_tables

SHIPMENT_SYSTEM_PROMPT = """\
You are a senior logistics and e-commerce operations analyst. You write Shipment Analysis
Reports for operations managers of a shipping platform, based on order and shipment data.

Cover the following areas, but only where the data supports them:
- Overall shipment status distribution and delivery performance.
- Cancellations and their recorded reasons.
- RTO (return to origin) and delivery exceptions (NDR, undelivered, lost, damaged).
- Courier partner performance comparison.
- Payment mode patterns (COD vs prepaid).
- Geographic concentration of orders.
- Operational bottlenecks, cost spikes, and courier routing anomalies.
- Data quality issues that affect the reliability of the analysis.

Executive Summary Guidelines:
- Write a 3-sentence high-level overview highlighting: (1) the main takeaway, (2) the single biggest logistics bottleneck found, and (3) the key operational/financial impact.

Financial requests:
- Use shipping_financial and relevant shipping-charge table references calculated from totalCharge.
- Report totals, courier/payment comparisons and cancelled-order charges only where available.
- Average shipping charge is over orders with valid charges; never substitute total records when charges are missing.
- Invoice/order value and collectableAmount are not shipping charges or revenue.
- Cancelled-order charges do not prove actual losses; do not invent profit, revenue or historical comparisons.
Interpretation notes:
- "status_category_breakdown" was derived by explicit backend status mappings; the raw
  values are listed in "status_breakdown". Mention this if categories look ambiguous.
- Courier rates use the total orders assigned to that courier as the denominator. Orders that
  are still pending or in transit are included, so rates are not final outcomes.
- Avoid strong conclusions about couriers, states or reasons with very few orders;
  call out the small sample size instead.
"""

DEFAULT_SHIPMENT_PROMPT = (
    "Provide an overall shipment performance analysis covering status distribution, "
    "cancellations and their reasons, RTO, courier performance and payment modes."
)

METRIC_DEFINITIONS = {
    "status_category_breakdown": "Orders grouped into delivered / in_transit / pending / cancelled / rto / exception / other / unknown by exact normalized backend status mappings.",
    "rates": "Category counts as a percentage of total_orders.",
    "cancellation_reasons": "Reasons recorded on orders in the 'cancelled' category; percent is of cancelled orders that have a reason.",
    "courier_performance": "Per courier: orders and outcome counts; rate fields are percentages of that courier's total orders.",
    "payment_mode_breakdown": "Orders by payment mode; values containing COD/cash are grouped as COD, prepaid/online as Prepaid.",
    "order_value": "Statistics over orders with a numeric invoice/order value. Currency is as provided in the source data.",
    "field_coverage": "How many orders contained each field (percent of total_orders).",
}

ACCEPTED_DATA_PATHS = ("data.getOrderList", "data.data.getOrderList", "data.orders")

# Logical field -> accepted source keys. Keys are compared after lower-casing and
# removing non-alphanumerics, so "order_status", "orderStatus" and "Order Status"
# all match "orderstatus". Nested objects are flattened by joining key names,
# e.g. {"consignee": {"state": ...}} becomes "consigneestate".
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "status": (
        "orderstatus", "shipmentstatus", "currentstatus", "status", "trackingstatus", "statusname",
        "orderstatusname", "shipmentstatusname", "currentstatusname", "deliverystatus",
    ),
    "courier": (
        "couriername", "courier", "carriername", "carrier", "courierpartner", "couriercompany",
        "courierpartnername", "lspname", "deliverypartner", "shippingpartner", "couriernameshipping",
    ),
    "payment_mode": (
        "paymenttype", "paymentmode", "paymentmethod", "paymentcategory", "codprepaid", "ordertypepayment",
    ),
    # Bigship sends orderCancelReason (structured) and orderCancelRemark (free text): reason first.
    "cancellation_reason": (
        "ordercancelreason", "cancellationreason", "cancelreason", "reasonforcancellation", "cancelledreason",
        "cancellationremark", "cancellationremarks", "cancelremark", "cancelremarks", "ordercancelremark",
    ),
    "destination_state": (
        "state", "deliverystate", "consigneestate", "shippingstate", "destinationstate", "customerstate",
        "dropstate", "consigneeaddressstate", "deliveryaddressstate", "shippingaddressstate",
        "customeraddressstate", "consigneedetailsstate", "customerdetailsstate",
    ),
    "destination_city": (
        "city", "deliverycity", "consigneecity", "shippingcity", "destinationcity", "customercity",
        "dropcity", "consigneeaddresscity", "deliveryaddresscity", "shippingaddresscity",
        "customeraddresscity", "consigneedetailscity", "customerdetailscity",
    ),
    "zone": ("zone", "shippingzone", "deliveryzone"),
    "order_date": (
        "orderdate", "createdat", "createddate", "orderedon", "ordercreateddate", "ordercreatedat",
        "bookingdate", "orderdatetime", "createdon",
    ),
    "order_value": (
        "invoicetotalamount", "invoiceamount", "invoicevalue", "ordervalue", "orderamount", "totalamount",
        "productvalue", "totalordervalue", "shipmentvalue",
    ),
    # Separate measures: never substituted for one another.
    "charge": ("totalcharge", "shippingcharge", "shippingcharges", "freightcharge", "freightcharges"),
    "collectable_amount": ("collectableamount", "collectablevalue", "codamount", "codcollectableamount"),
    "pickup_date": ("pickupdate", "pickeddate", "pickedupdate", "pickupdatetime", "pickupdoneat"),
    "delivery_date": ("deliverydate", "delivereddate", "deliveredat", "deliverydatetime", "deliveredon"),
    "weight": ("weightkg", "weight", "chargeableweight", "deadweight", "totalweight", "actualweight", "packageweight"),
    "order_id": ("orderid", "mastercustomorderid", "order_id", "customorderid", "id"),
    "awb_number": ("awbnumber", "awb_number", "awbno", "referencenumber", "reference_number", "lrnumber", "lr_number", "trackingnumber"),
    "product_name": ("productsname", "products_name", "productname", "product_name", "itemname", "item_name", "description"),
}

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d\s\-()]{7,15}\d(?!\w)")
_WHITESPACE_RE = re.compile(r"\s+")
_NUMBER_CLEAN_RE = re.compile(r"(?i)inr|rs\.?|₹|kgs?|,|\s")

_DATE_FORMATS = (
    "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d",
    "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M",
    "%d-%m-%Y %I:%M %p", "%d/%m/%Y %I:%M %p", "%d-%m-%Y %I:%M:%S %p", "%d/%m/%Y %I:%M:%S %p",
    "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d %b %Y %H:%M", "%d %b %Y %I:%M %p", "%b %d, %Y",
)

# Lost is its own outcome: Bigship reports "lost" and "undelivered" as separate account counters.
STATUS_CATEGORIES = ("delivered", "in_transit", "pending", "cancelled", "rto", "exception", "lost", "other", "unknown")
_CATEGORY_LABELS = {
    "delivered": "Delivered",
    "in_transit": "In Transit",
    "pending": "Pending / Not Shipped",
    "cancelled": "Cancelled",
    "rto": "RTO / Returned",
    "exception": "Delivery Exception (NDR)",
    "lost": "Lost",
    "other": "Other",
    "unknown": "Status Missing",
}
# --- Extraction & sanitisation ----------------------------------------------

def extract_orders(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Locate the order list in the payload sent by Angular and validate it."""
    candidate: Any = None
    for container in (data, data.get("data") if isinstance(data.get("data"), dict) else None):
        if not container:
            continue
        for key in ("getOrderList", "orders", "order_list", "shipments"):
            if key in container and container[key] is not None:
                candidate = container[key]
                break
        if candidate is not None:
            break

    # Deep fallback search if not found in top-level containers
    if candidate is None:
        def _find_orders_recursive(obj: Any, depth: int = 0) -> Any:
            if depth > 4 or not isinstance(obj, dict):
                return None
            for k in ("getOrderList", "orders", "order_list", "shipments"):
                if k in obj and obj[k] is not None:
                    return obj[k]
            for v in obj.values():
                if isinstance(v, dict):
                    found = _find_orders_recursive(v, depth + 1)
                    if found is not None:
                        return found
            return None
        candidate = _find_orders_recursive(data)

    # Paginated shape: {"getOrderList": {"data": [...], "total": 120}}
    if isinstance(candidate, dict):
        candidate = next(
            (candidate[k] for k in ("data", "orders", "items", "records", "list", "rows")
             if isinstance(candidate.get(k), list)),
            candidate,
        )

    if candidate is None:
        raise InvalidReportDataError(
            "Shipment data must contain an order list at data.getOrderList.",
            details={"accepted_paths": list(ACCEPTED_DATA_PATHS)},
        )
    if not isinstance(candidate, list):
        raise InvalidReportDataError("data.getOrderList must be a list of order objects.")
    if not candidate:
        raise InvalidReportDataError("data.getOrderList contains no orders to analyse.")
    if len(candidate) > settings.max_records:
        raise InvalidReportDataError(
            f"Too many orders: {len(candidate)} supplied, the maximum is {settings.max_records}.",
            details={"received": len(candidate), "max_records": settings.max_records},
        )

    invalid = [index for index, item in enumerate(candidate) if not isinstance(item, dict)]
    if invalid:
        raise InvalidReportDataError(
            f"{len(invalid)} item(s) in data.getOrderList are not order objects.",
            details={"invalid_indexes": invalid[:20]},
        )
    return candidate


_ORDER_KEYS = ("getOrderList", "orders", "order_list", "shipments")
_PAGE_KEYS = ("data", "orders", "items", "records", "list", "rows")


def _orders_path(data: dict[str, Any]) -> tuple[Any, ...] | None:
    """Key path to the order list, found with the same precedence as ``extract_orders``."""
    def in_container(container, prefix):
        for key in _ORDER_KEYS:
            if key in container and container[key] is not None:
                return prefix + (key,)
        return None

    path = in_container(data, ())
    if path is None and isinstance(data.get("data"), dict):
        path = in_container(data["data"], ("data",))
    if path is None:
        def search(obj, prefix, depth):
            if depth > 4 or not isinstance(obj, dict):
                return None
            found = in_container(obj, prefix)
            for key, value in obj.items():
                if found is not None:
                    break
                if isinstance(value, dict):
                    found = search(value, prefix + (key,), depth + 1)
            return found
        path = search(data, (), 0)
    if path is None:
        return None
    node = data
    for key in path:
        node = node[key]
    if isinstance(node, dict):
        inner = next((k for k in _PAGE_KEYS if isinstance(node.get(k), list)), None)
        if inner is not None:
            path += (inner,)
    return path


def replace_orders(data: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    """Shallow copy of the payload whose order list is ``records``; every other key is kept as supplied."""
    path = _orders_path(data)
    if path is None:
        raise InvalidReportDataError("Shipment data must contain an order list at data.getOrderList.")

    def rebuild(node, keys):
        copy = dict(node)
        copy[keys[0]] = list(records) if len(keys) == 1 else rebuild(node[keys[0]], keys[1:])
        return copy

    return rebuild(data, path)


def _norm_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _flatten(record: dict[str, Any], prefix: str = "", depth: int = 0, out: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {} if out is None else out
    for key, value in record.items():
        flat_key = prefix + _norm_key(key)
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
            value = value[0]  # e.g. "consignee": [{"city": ...}]; longer lists are not attributable to one field
        if isinstance(value, dict):
            if depth < 3:
                _flatten(value, flat_key, depth + 1, out)
        elif not isinstance(value, list):
            out.setdefault(flat_key, value)
    return out


def scrub_text(value: Any, max_length: int = 120) -> str | None:
    """Convert a value to a short, single-line string with emails/phone numbers removed."""
    if value is None or isinstance(value, (dict, list)):
        return None
    text = _WHITESPACE_RE.sub(" ", str(value)).strip()
    if not text or text.lower() in {"null", "none", "undefined", "n/a", "na", "-"}:
        return None
    text = _EMAIL_RE.sub("[redacted]", text)
    text = _PHONE_RE.sub("[redacted]", text)
    if len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "…"
    return text


def parse_date(value: Any) -> date | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        timestamp = value / 1000 if value > 1e11 else value
        if 946684800 <= timestamp <= 4102444800:  # 2000-01-01 .. 2100-01-01
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).date()
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return parse_date(int(text))
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        try:
            number = float(_NUMBER_CLEAN_RE.sub("", str(value)))
        except ValueError:
            return None
    return number if math.isfinite(number) and number >= 0 else None


def categorize_status(status: str | None) -> str:
    if not status:
        return "unknown"
    text = re.sub(r"[_-]+", " ", status.casefold()).strip()
    return STATUS_MAPPING.get(text, "other")


# Explicit text statuses supported by this backend. Unknown codes are not guessed.
STATUS_MAPPING = {
    **dict.fromkeys(("delivered", "delivery completed", "successfully delivered"), "delivered"),
    **dict.fromkeys(("cancelled", "canceled", "cancel", "order cancelled", "shipment cancelled"), "cancelled"),
    **dict.fromkeys(("in transit", "out for delivery", "shipped", "dispatched", "picked up", "picked", "reached hub"), "in_transit"),
    **dict.fromkeys(("pending", "new", "booked", "pickup pending", "pickup scheduled", "not picked", "not picked up",
                     "not pickup", "unshipped", "manifested", "processing", "created", "ready to ship"), "pending"),
    **dict.fromkeys(("rto", "rto initiated", "rto in transit", "rto delivered", "returned", "return to origin"), "rto"),
    **dict.fromkeys(("ndr", "undelivered", "not delivered", "failed", "damaged", "exception", "delivery failed"), "exception"),
    **dict.fromkeys(("lost", "shipment lost", "lost in transit"), "lost"),
}


def normalize_payment(value: str | None) -> str | None:
    if not value:
        return None
    text = value.lower().replace("-", "").replace(" ", "")
    if text in {"topay", "to_pay"}:
        return "ToPay"
    if "cod" in text or "cash" in text:
        return "COD"
    if "prepaid" in text or "online" in text or text == "paid":
        return "Prepaid"
    return value


@dataclass
class ShipmentOrder:
    status: str | None
    category: str
    courier: str | None
    payment_mode: str | None
    cancellation_reason: str | None
    destination_state: str | None
    destination_city: str | None
    zone: str | None
    order_date: date | None
    order_value: float | None
    weight: float | None
    order_id: str | None = None
    awb_number: str | None = None
    product_name: str | None = None
    charge: float | None = None
    collectable_amount: float | None = None
    pickup_date: date | None = None
    delivery_date: date | None = None

    def to_sample(self) -> dict[str, Any]:
        """Sanitised representation sent to the AI as a qualitative example."""
        sample = {
            "order_id": self.order_id,
            "awb_number": self.awb_number,
            "product_name": self.product_name,
            "status": self.status,
            "status_category": self.category,
            "courier": self.courier,
            "payment_mode": self.payment_mode,
            "cancellation_reason": self.cancellation_reason,
            "destination_state": self.destination_state,
            "destination_city": self.destination_city,
            "zone": self.zone,
            "order_date": self.order_date.isoformat() if self.order_date else None,
            "order_value": self.order_value,
            "shipping_charge": self.charge,
            "weight": self.weight,
        }
        return {key: value for key, value in sample.items() if value is not None}


# Nested destination keys not in the alias list ("consigneeaddressdetailscity", "receivercity"). Only
# recipient-side prefixes qualify, so pickup, warehouse or seller cities are never read as destinations.
_DESTINATION_KEY = {
    "destination_city": re.compile(r"^(?:consignee|delivery|shipping|customer|drop|destination|receiver|buyer|shipto)[a-z0-9]*city$"),
    "destination_state": re.compile(r"^(?:consignee|delivery|shipping|customer|drop|destination|receiver|buyer|shipto)[a-z0-9]*state$"),
}


def _lookup(flat: dict[str, Any], field: str) -> Any:
    for alias in FIELD_ALIASES[field]:
        value = flat.get(alias)
        if value is not None and value != "":
            return value
    pattern = _DESTINATION_KEY.get(field)
    if pattern:
        return next((v for k, v in flat.items() if pattern.match(k) and v not in (None, "")
                     and not re.search(r"pickup|origin|seller|warehouse|return", k)), None)
    return None


def parse_order(record: dict[str, Any]) -> ShipmentOrder:
    flat = _flatten(record)
    status = scrub_text(_lookup(flat, "status"), 80)
    return ShipmentOrder(
        status=status,
        category=categorize_status(status),
        courier=scrub_text(_lookup(flat, "courier"), 80),
        payment_mode=normalize_payment(scrub_text(_lookup(flat, "payment_mode"), 40)),
        cancellation_reason=scrub_text(_lookup(flat, "cancellation_reason"), 160),
        destination_state=scrub_text(_lookup(flat, "destination_state"), 60),
        destination_city=scrub_text(_lookup(flat, "destination_city"), 60),
        zone=scrub_text(_lookup(flat, "zone"), 30),
        order_date=parse_date(_lookup(flat, "order_date")),
        order_value=parse_number(_lookup(flat, "order_value")),
        weight=parse_number(_lookup(flat, "weight")),
        order_id=scrub_text(_lookup(flat, "order_id"), 40),
        awb_number=scrub_text(_lookup(flat, "awb_number"), 40),
        product_name=scrub_text(_lookup(flat, "product_name"), 80),
        charge=parse_number(_lookup(flat, "charge")),
        collectable_amount=parse_number(_lookup(flat, "collectable_amount")),
        pickup_date=parse_date(_lookup(flat, "pickup_date")),
        delivery_date=parse_date(_lookup(flat, "delivery_date")),
    )


# --- Metrics -----------------------------------------------------------------

def _pct(part: int | float, whole: int | float) -> float:
    return round(part * 100 / whole, 1) if whole else 0.0


def _group_counts(values: Iterable[str | None]) -> list[tuple[str, int]]:
    """Count values case-insensitively, keeping the first spelling seen."""
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    for value in values:
        if not value:
            continue
        key = value.casefold()
        counts[key] += 1
        display.setdefault(key, value)
    return [(display[key], count) for key, count in counts.most_common()]


def _breakdown(values: Iterable[str | None], label: str, total: int, limit: int) -> list[dict[str, Any]]:
    return [
        {label: value, "count": count, "percent": _pct(count, total)}
        for value, count in _group_counts(values)[:limit]
    ]


def compute_metrics(orders: list[ShipmentOrder]) -> dict[str, Any]:
    total = len(orders)
    category_counts = Counter(order.category for order in orders)

    coverage_fields = {
        "status": lambda o: o.status, "courier": lambda o: o.courier, "payment_mode": lambda o: o.payment_mode,
        "cancellation_reason": lambda o: o.cancellation_reason, "destination_state": lambda o: o.destination_state,
        "destination_city": lambda o: o.destination_city, "zone": lambda o: o.zone,
        "order_date": lambda o: o.order_date, "order_value": lambda o: o.order_value, "weight": lambda o: o.weight,
    }
    field_coverage = {}
    for name, getter in coverage_fields.items():
        present = sum(1 for order in orders if getter(order) is not None)
        field_coverage[name] = {"present": present, "percent": _pct(present, total)}

    # Courier performance
    courier_stats: dict[str, Counter[str]] = defaultdict(Counter)
    courier_names: dict[str, str] = {}
    for order in orders:
        name = order.courier or "Not specified"
        key = name.casefold()
        courier_names.setdefault(key, name)
        courier_stats[key]["total"] += 1
        courier_stats[key][order.category] += 1
    courier_performance = []
    for key, stats in sorted(courier_stats.items(), key=lambda item: -item[1]["total"]):
        courier_total = stats["total"]
        courier_performance.append({
            "courier": courier_names[key],
            "total_orders": courier_total,
            "share_percent": _pct(courier_total, total),
            "delivered": stats["delivered"],
            "in_transit": stats["in_transit"],
            "pending": stats["pending"],
            "cancelled": stats["cancelled"],
            "rto": stats["rto"],
            "exception": stats["exception"],
            "lost": stats["lost"],
            "delivery_rate_percent": _pct(stats["delivered"], courier_total),
            "cancellation_rate_percent": _pct(stats["cancelled"], courier_total),
            "rto_rate_percent": _pct(stats["rto"], courier_total),
        })

    # Cancellation reasons
    cancelled_orders = [order for order in orders if order.category == "cancelled"]
    reasons = [order.cancellation_reason for order in cancelled_orders]
    with_reason = sum(1 for reason in reasons if reason)
    cancellation_reasons = {
        "cancelled_orders": len(cancelled_orders),
        "cancelled_orders_with_reason": with_reason,
        "top_reasons": [
            {"reason": reason, "count": count, "percent": _pct(count, with_reason)}
            for reason, count in _group_counts(reasons)[:10]
        ],
    }

    # Dates
    dates = [order.order_date for order in orders if order.order_date]
    order_date_range = None
    if dates:
        order_date_range = {
            "from": min(dates).isoformat(),
            "to": max(dates).isoformat(),
            "orders_with_valid_date": len(dates),
        }

    # Values
    values = [order.order_value for order in orders if order.order_value is not None]
    order_value = None
    if values:
        order_value = {
            "orders_with_value": len(values),
            "total": round(sum(values), 2),
            "average": round(sum(values) / len(values), 2),
            "min": round(min(values), 2),
            "max": round(max(values), 2),
        }
    weights = [order.weight for order in orders if order.weight is not None]
    weight = None
    if weights:
        weight = {
            "orders_with_weight": len(weights),
            "average": round(sum(weights) / len(weights), 3),
            "unit_note": "Unit as provided in the source data.",
        }

    metrics: dict[str, Any] = {
        "total_orders": total,
        "status_category_breakdown": [
            {"category": category, "label": _CATEGORY_LABELS[category],
             "count": category_counts[category], "percent": _pct(category_counts[category], total)}
            for category in STATUS_CATEGORIES
            if category_counts[category]
        ],
        "rates": {
            f"{category}_percent": _pct(category_counts[category], total)
            for category in ("delivered", "in_transit", "pending", "cancelled", "rto", "exception", "lost")
        },
        "status_breakdown": _breakdown((o.status for o in orders), "status", total, 15),
        "cancellation_reasons": cancellation_reasons,
        "courier_performance": courier_performance,
        "distinct_couriers": len([k for k in courier_stats if courier_names[k] != "Not specified"]),
        "payment_mode_breakdown": _breakdown((o.payment_mode for o in orders), "payment_mode", total, 10),
        "top_destination_states": _breakdown((o.destination_state for o in orders), "state", total, 10),
        "top_destination_cities": _breakdown((o.destination_city for o in orders), "city", total, 10),
        "zone_breakdown": _breakdown((o.zone for o in orders), "zone", total, 10),
        "order_date_range": order_date_range,
        "order_value": order_value,
        "weight": weight,
        "field_coverage": field_coverage,
    }
    metrics["derived_percentages"] = derived_percentages(metrics)
    metrics["data_quality_notes"] = _data_quality_notes(metrics)
    metrics["data_quality_notes"].append("Raw status tables show up to 15 labels; reasons, payment modes and destinations show up to 10 groups; courier tables and charts show up to 200 couriers. Omitted groups are not zero.")
    return metrics


def _data_quality_notes(metrics: dict[str, Any]) -> list[str]:
    total = metrics["total_orders"]
    coverage = metrics["field_coverage"]
    notes: list[str] = []
    labels = {
        "status": "shipment status", "courier": "courier name", "payment_mode": "payment mode",
        "destination_state": "destination state", "order_date": "order date", "order_value": "order value",
    }
    for field, label in labels.items():
        present = coverage[field]["present"]
        if present == 0:
            notes.append(f"No {label} field was found in the data.")
        elif present < total:
            notes.append(f"{label.capitalize()} is missing for {total - present} of {total} orders.")

    reasons = metrics["cancellation_reasons"]
    if reasons["cancelled_orders"] and reasons["cancelled_orders_with_reason"] < reasons["cancelled_orders"]:
        notes.append(
            f"{reasons['cancelled_orders'] - reasons['cancelled_orders_with_reason']} of "
            f"{reasons['cancelled_orders']} cancelled orders have no cancellation reason recorded."
        )
    other = next((c["count"] for c in metrics["status_category_breakdown"] if c["category"] == "other"), 0)
    if other:
        notes.append(f"{other} orders have a status that could not be mapped to a standard category.")
    if total < 30:
        notes.append(f"The dataset is small ({total} orders); percentages may not be representative.")
    return notes


def _sample_records(orders: list[ShipmentOrder], limit: int) -> list[dict[str, Any]]:
    """Round-robin across status categories so the sample shows variety."""
    if limit <= 0:
        return []
    by_category: dict[str, list[ShipmentOrder]] = defaultdict(list)
    for order in orders:
        by_category[order.category].append(order)
    queues = [list(reversed(items)) for items in by_category.values()]
    sample: list[dict[str, Any]] = []
    while len(sample) < limit and any(queues):
        for queue in queues:
            if queue and len(sample) < limit:
                sample.append(queue.pop().to_sample())
    return sample


# --- Presentation (Python-calculated, never AI-generated) --------------------

def _fmt_int(value: int) -> str:
    return f"{value:,}"


def _fmt_date(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d %b %Y")


def build_key_metrics(metrics: dict[str, Any]) -> list[KeyMetric]:
    total = metrics["total_orders"]
    by_category = {item["category"]: item for item in metrics["status_category_breakdown"]}
    items = [KeyMetric(label="Total Orders", value=_fmt_int(total))]

    for category in ("delivered", "in_transit", "pending", "cancelled", "rto", "exception", "lost"):
        entry = by_category.get(category)
        if entry or category in ("delivered", "cancelled", "rto"):
            count = entry["count"] if entry else 0
            items.append(KeyMetric(label=_CATEGORY_LABELS[category], value=f"{_fmt_int(count)} ({_pct(count, total)}%)"))

    if metrics["distinct_couriers"]:
        items.append(KeyMetric(label="Courier Partners", value=str(metrics["distinct_couriers"])))

    top_reasons = metrics["cancellation_reasons"]["top_reasons"]
    if top_reasons:
        top = top_reasons[0]
        items.append(KeyMetric(
            label="Top Cancellation Reason",
            value=top["reason"],
            note=f"{top['count']} of {metrics['cancellation_reasons']['cancelled_orders_with_reason']} cancellations with a reason",
        ))

    cod = next((p for p in metrics["payment_mode_breakdown"] if p["payment_mode"] == "COD"), None)
    if metrics["field_coverage"]["payment_mode"]["present"]:
        items.append(KeyMetric(label="COD Orders", value=f"{_fmt_int(cod['count'] if cod else 0)} ({cod['percent'] if cod else 0.0}%)"))

    if metrics["order_value"]:
        value = metrics["order_value"]
        items.append(KeyMetric(
            label="Total Order Value",
            value=f"{value['total']:,.2f}",
            note=f"Across {value['orders_with_value']} orders; currency as provided in source data",
        ))

    if metrics["order_date_range"]:
        date_range = metrics["order_date_range"]
        items.append(KeyMetric(label="Order Date Range", value=f"{_fmt_date(date_range['from'])} – {_fmt_date(date_range['to'])}"))
    return items


def build_tables(metrics: dict[str, Any]) -> list[ReportTable]:
    tables = [
        ReportTable(
            title="Shipment Status Summary",
            columns=["Status Category", "Orders", "Share (%)"],
            rows=[[c["label"], _fmt_int(c["count"]), f"{c['percent']}"] for c in metrics["status_category_breakdown"]],
        )
    ]
    if metrics["courier_performance"] and metrics["distinct_couriers"]:
        tables.append(ReportTable(
            title="Courier Performance",
            columns=["Courier", "Orders", "Delivered", "Cancelled", "RTO", "Delivery Rate (%)"],
            rows=[
                [c["courier"], _fmt_int(c["total_orders"]), _fmt_int(c["delivered"]), _fmt_int(c["cancelled"]),
                 _fmt_int(c["rto"]), f"{c['delivery_rate_percent']}"]
                for c in metrics["courier_performance"][:200]
            ],
        ))
    reasons = metrics["cancellation_reasons"]
    if reasons["top_reasons"]:
        tables.append(ReportTable(
            title="Top Cancellation Reasons",
            columns=["Reason", "Orders", "Share of Cancellations with Reason (%)"],
            rows=[[r["reason"], _fmt_int(r["count"]), f"{r['percent']}"] for r in reasons["top_reasons"]],
        ))
    if metrics["payment_mode_breakdown"]:
        tables.append(ReportTable(
            title="Payment Mode",
            columns=["Payment Mode", "Orders", "Share (%)"],
            rows=[[p["payment_mode"], _fmt_int(p["count"]), f"{p['percent']}"] for p in metrics["payment_mode_breakdown"]],
        ))
    if metrics["top_destination_states"]:
        tables.append(ReportTable(
            title="Top Destination States",
            columns=["State", "Orders", "Share (%)"],
            rows=[[s["state"], _fmt_int(s["count"]), f"{s['percent']}"] for s in metrics["top_destination_states"]],
        ))
    return tables


# --- Entry point ---------------------------------------------------------------

def analyze_shipments(data: dict[str, Any]) -> AnalysisResult:
    records = extract_orders(data)
    orders = [parse_order(record) for record in records]
    metrics = compute_metrics(orders)
    metrics["shipping_financial"] = shipping_financial_metrics(records, orders)
    financial = metrics["shipping_financial"]
    metrics["data_quality_notes"].append(
        "Shipping charges use only valid totalCharge values. Missing or invalid values are excluded, not zero. "
        "Averages use orders with valid charges. Invoice value and collectableAmount are separate measures. "
        "Charges on cancelled orders are not verified losses or revenue. Financial tables show at most 200 groups."
    )
    tables = build_tables(metrics) + financial_tables(financial)
    container = data.get("data") if isinstance(data.get("data"), dict) else data
    acc = data.get("account_summary") or (container.get("account_summary") if isinstance(container, dict) else None) or {}
    if not isinstance(acc, dict):
        acc = {}
    if not acc and isinstance(container, dict) and any(k in container for k in ("all_orders", "delivered", "cancel", "in_transit")):
        acc = {k: container[k] for k in ("all_orders", "delivered", "cancel", "in_transit", "undelivered", "rto_in_transit", "rto_delivered", "lost") if k in container}
    if isinstance(acc, dict) and isinstance(acc.get("all_orders"), (int, float)) and acc["all_orders"] > 0:
        all_ord = acc["all_orders"]
        # Account counters come from the API, not the supplied records. A counter the API did not send is
        # left out rather than shown as 0; the share denominator is the API's all_orders.
        bench_rows = []
        for key, label, note in (("delivered", "Delivered", "Completed successfully"),
                                 ("cancel", "Cancelled", "Cancelled before delivery"),
                                 ("in_transit", "In Transit", "Active on carrier network")):
            if isinstance(acc.get(key), (int, float)):
                bench_rows.append([label, _fmt_int(acc[key]), f"{_pct(acc[key], all_ord)}", note])
        if any(isinstance(acc.get(k), (int, float)) for k in ("rto_in_transit", "rto_delivered")):
            rto = acc.get("rto_in_transit", 0) + acc.get("rto_delivered", 0)
            bench_rows.append(["RTO (In Transit + Delivered)", _fmt_int(rto), f"{_pct(rto, all_ord)}", "Returned / Returning to origin"])
        if acc.get("undelivered", 0) > 0:
            bench_rows.append(["Undelivered (NDR)", _fmt_int(acc["undelivered"]), f"{_pct(acc['undelivered'], all_ord)}", "Failed delivery attempt, needs seller action"])
        if acc.get("lost", 0) > 0:
            bench_rows.append(["Lost", _fmt_int(acc["lost"]), f"{_pct(acc['lost'], all_ord)}", "Claim eligible with carrier"])
        tables.append(ReportTable(
            title="Account-Level Status Benchmark",
            columns=["Status Category", "Account Orders", "Share (%)", "Operational Benchmark"],
            rows=bench_rows,
        ))
        metrics["account_summary"] = acc
        rto_total = acc.get("rto", 0) or (acc.get("rto_in_transit", 0) + acc.get("rto_delivered", 0))
        mix = []
        if acc.get("in_transit", 0) > 0:
            mix.append({"status": "In Transit", "orders": acc["in_transit"], "share": _pct(acc["in_transit"], all_ord)})
        if acc.get("delivered", 0) > 0:
            mix.append({"status": "Delivered", "orders": acc["delivered"], "share": _pct(acc["delivered"], all_ord)})
        if rto_total > 0:
            mix.append({"status": "RTO", "orders": rto_total, "share": _pct(rto_total, all_ord)})
        if acc.get("cancel", 0) > 0:
            mix.append({"status": "Cancelled", "orders": acc["cancel"], "share": _pct(acc["cancel"], all_ord)})
        if acc.get("undelivered", 0) > 0:
            mix.append({"status": "Undelivered", "orders": acc["undelivered"], "share": _pct(acc["undelivered"], all_ord)})
        if acc.get("lost", 0) > 0:
            mix.append({"status": "Lost", "orders": acc["lost"], "share": _pct(acc["lost"], all_ord)})
        metrics["account_status_mix"] = mix
    source = container.get("getOrderList", container.get("orders")) if isinstance(container, dict) else None
    total = source.get("total") if isinstance(source, dict) else None
    metrics["scope"] = {
        "kind": "paginated" if isinstance(source, dict) else ("sample" if data.get("dataset_scope") == "sample" else "supplied_records"),
        "records_analyzed": len(orders),
        "reported_total": total if isinstance(total, int) and not isinstance(total, bool) and total >= len(orders) else None,
        "denominator": "supplied_records",
    }
    metrics["data_quality_notes"].append("All rates describe supplied records only; external summary counters are not combined. Full dataset coverage is not assumed.")
    sample = _sample_records(orders, settings.ai_sample_records)
    ai_context = {
        "dataset": "shipment_orders",
        "metric_definitions": METRIC_DEFINITIONS,
        "metrics": metrics,
        "available_tables": {f"table_{i}": t.model_dump() for i, t in enumerate(tables)},
    }
    if sample:
        ai_context["sample_records"] = sample
        ai_context["sample_note"] = (
            f"{len(sample)} sanitised records picked across status categories for illustration only; "
            "not representative of proportions."
        )
    return AnalysisResult(
        records_analyzed=len(orders),
        metrics=metrics,
        key_metrics=build_key_metrics(metrics),
        tables=tables,
        ai_context=ai_context,
    )


SHIPMENT_REPORT = ReportDefinition(
    report_type="shipment",
    display_name="Shipment Analysis Report",
    description="Shipment status, cancellations, RTO, courier performance, payment mode and destination analysis.",
    system_prompt=SHIPMENT_SYSTEM_PROMPT,
    default_prompt=DEFAULT_SHIPMENT_PROMPT,
    analyze=analyze_shipments,
    accepted_data_paths=ACCEPTED_DATA_PATHS,
)
