"""Deterministic shipment insights (report schema 3.0).

Every number here is calculated in Python from the supplied orders and the account summary
counters. DeepSeek never produces these values; Angular renders them as they are.
Reference: tests/fixtures/report_v3/README.md (the target dashboard's rules).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from app.prompts.shipment_prompt import _flatten, _norm_key, categorize_status, extract_orders, parse_date, scrub_text

SCHEMA_VERSION = "3.0"
OVER_DAYS = 30
OUTLIER_FACTOR = Decimal(3)

# Logical field -> accepted keys (compared after lower-casing and removing non-alphanumerics).
# Raw Bigship order-list names first, then the flattened names used by the reference fixture.
ALIASES: dict[str, tuple[str, ...]] = {
    "order_id": ("mastercustomorderid", "orderid", "ordernumber", "referencenumber"),
    "order_date": ("orderdate", "createdat", "ordercreateddate", "manifestdatetime", "bookingdate"),
    "courier": ("couriername", "courier", "carriername", "courierpartner"),
    "product": ("productsname", "productname", "product", "productdetails"),
    "dest_city": ("consigneecity", "destinationcity", "destcity", "deliverycity", "city"),
    "dest_state": ("consigneestate", "destinationstate", "deststate", "deliverystate", "state"),
    "status": ("status", "orderstatus", "shipmentstatus", "currentstatus"),
    "payment_mode": ("paymentmode", "paymenttype", "paymentmethod"),
    "weight": ("weightkg", "totalweight", "chargeableweight", "chargedweight", "weight"),
    "weight_unit": ("weightunit",),
    "shipping_charge": ("totalcharge", "shippingcharge", "shippingcharges", "freightcharge"),
    "invoice_value": ("invoicetotalamount", "invoicevalue", "invoiceamount", "ordervalue"),
    "eway_bill": ("ewaybill", "ewaybillno", "ewaybillnumber", "ewaybillfileurl", "ewaybilldocument"),
    "pickup_date": ("pickupdate", "pickeddate", "pickedupdate", "pickupdatetime", "pickupdoneat"),
    "delivery_date": ("deliverydate", "delivereddate", "deliveryedate", "deliveredat", "deliverydatetime"),
    "courier_remark": ("courierremark", "courierremarks", "lastremark", "trackingremark", "statusremark"),
    "cancel_remark": ("ordercancelremark", "cancelremark", "cancellationremark"),
    "cancel_reason": ("ordercancelreason", "cancelreason", "cancellationreason"),
}

# Account summary counters in the order-list response -> display label (dashboard order).
ACCOUNT_COUNTERS = (
    ("delivered", "Delivered"), ("cancel", "Cancelled"), ("in_transit", "In transit"),
    ("pickup_scheduled", "Pickup scheduled"), ("not_pickup", "Not picked"), ("undelivered", "Undelivered"),
    ("rto_in_transit", "RTO in transit"), ("out_for_delivery", "Out for delivery"),
    ("rto_delivered", "RTO delivered"), ("lost", "Lost"),
)

THEME = {
    "brand": {"primary": "#6C4CF1", "primary_soft": "#EEEBFF"},
    "status": {"ok": "#16A34A", "warn": "#F59E0B", "bad": "#EF4444", "neutral": "#9AA1B2"},
    "severity": {"High": "#EF4444", "Medium": "#F59E0B", "Low": "#9AA1B2"},
    "series": ["#6C4CF1", "#16A34A", "#F59E0B", "#3B82F6", "#EF4444", "#14B8A6", "#8B5CF6", "#9AA1B2"],
}


def _money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).replace(",", "").replace("₹", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number >= 0 else None


def _r2(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _num(value: Decimal) -> float | int:
    return int(value) if value == value.to_integral_value() else _r2(value)


def _median_upper(values: list) -> Any:
    """Same rule as the reference dashboard: sorted[n // 2]."""
    ordered = sorted(values)
    return ordered[len(ordered) // 2] if ordered else None


def _pick(flat: dict[str, Any], field: str) -> Any:
    for key in ALIASES[field]:
        value = flat.get(key)
        if value not in (None, "", "null", "NULL"):
            return value
    return None


def normalize_order(record: dict[str, Any]) -> dict[str, Any]:
    """One allow-listed, PII-free row. Names, phones, emails and addresses are never read."""
    flat = _flatten(record)
    weight = _money(_pick(flat, "weight"))
    unit = str(_pick(flat, "weight_unit") or "").strip().lower()
    if weight is not None and unit in {"g", "gm", "gms", "gram", "grams"}:
        weight = weight / 1000
    order_date = parse_date(_pick(flat, "order_date"))
    pickup = parse_date(_pick(flat, "pickup_date"))
    delivery = parse_date(_pick(flat, "delivery_date"))
    eway = _pick(flat, "eway_bill")
    return {
        # Business identifier shown in the report (not personal data); the phone scrubber would
        # mistake 9-digit order IDs for phone numbers. Never sent to the AI.
        "order_id": (re.sub(r"[^A-Za-z0-9_\-/]", "", str(_pick(flat, "order_id")))[:40] or None) if _pick(flat, "order_id") is not None else None,
        "order_date": order_date.isoformat() if order_date else None,
        "courier": scrub_text(_pick(flat, "courier"), 80) or "Not specified",
        "product": scrub_text(_pick(flat, "product"), 80),
        "dest_city": scrub_text(_pick(flat, "dest_city"), 60),
        "dest_state": scrub_text(_pick(flat, "dest_state"), 60),
        "status": scrub_text(_pick(flat, "status"), 60),
        "payment_mode": scrub_text(_pick(flat, "payment_mode"), 30),
        "weight_kg": weight,
        "shipping_charge": _money(_pick(flat, "shipping_charge")),
        "invoice_value": _money(_pick(flat, "invoice_value")),
        "eway_bill": None if eway is None else bool(str(eway).strip()) and str(eway).strip().lower() not in {"no", "false", "0"},
        "pickup_date": pickup.isoformat() if pickup else None,
        "delivery_date": delivery.isoformat() if delivery else None,
        "courier_remark": scrub_text(_pick(flat, "courier_remark"), 160),
        "cancel_remark": scrub_text(_pick(flat, "cancel_remark"), 160),
        "cancel_reason": scrub_text(_pick(flat, "cancel_reason"), 160),
        "_present": {f: _pick(flat, f) is not None for f in ALIASES},
    }


def account_status(summary: Any) -> tuple[list[dict[str, Any]], int | None]:
    """Account-level counters (all orders, not just the filtered rows)."""
    rows: list[dict[str, Any]] = []
    total = None
    if isinstance(summary, list):  # [{status, orders}] (reference fixture)
        rows = [{"status": str(r.get("status")), "orders": int(r.get("orders") or 0)} for r in summary if isinstance(r, dict)]
    elif isinstance(summary, dict):
        for key, label in ACCOUNT_COUNTERS:
            if summary.get(key) is not None:
                try:
                    rows.append({"status": label, "orders": int(summary[key])})
                except (TypeError, ValueError):
                    continue
        try:
            total = int(summary["all_orders"]) if summary.get("all_orders") is not None else None
        except (TypeError, ValueError):
            total = None
    if not rows:
        return [], None
    total = total if total is not None and total > 0 else sum(r["orders"] for r in rows)
    mix = [{"status": r["status"], "orders": r["orders"], "share": round(r["orders"] / total, 4) if total else 0.0}
           for r in rows if r["orders"] > 0]
    return mix, total


def _fmt_day(value: str | None) -> str:
    if not value:
        return ""
    day = date.fromisoformat(value[:10])
    return f"{day.day} {day.strftime('%b %Y')}"  # portable (no %-d on Windows)


def build_insights(data: dict[str, Any], as_of: date | None = None) -> dict[str, Any]:
    records = extract_orders(data)
    container = data.get("data") if isinstance(data.get("data"), dict) else data
    report_filter = container.get("report_filter") if isinstance(container.get("report_filter"), dict) else {}
    as_of = as_of or parse_date(container.get("as_of")) or datetime.now(timezone.utc).date()
    rows = [normalize_order(r) for r in records]
    n = len(rows)
    present = {f: any(r["_present"][f] for r in rows) for f in ALIASES}

    charges = [r["shipping_charge"] for r in rows if r["shipping_charge"] is not None]
    median_charge = _median_upper(charges)
    checks = []
    for r in rows:
        age = (as_of - date.fromisoformat(r["order_date"])).days if r["order_date"] else None
        flags = {
            "delivered_date": bool(r["delivery_date"]) if present["delivery_date"] else None,
            "cancel_note": bool(r["cancel_remark"] or r["cancel_reason"]) if (present["cancel_remark"] or present["cancel_reason"]) else None,
            "pickup_before_order": (bool(r["pickup_date"] and r["order_date"] and r["pickup_date"] < r["order_date"])
                                    if present["pickup_date"] else None),
            "charge_outlier": (r["shipping_charge"] is not None and median_charge is not None
                               and r["shipping_charge"] > OUTLIER_FACTOR * median_charge),
            "over_30_days": age is not None and age > OVER_DAYS,
        }
        checks.append({"order_id": r["order_id"], "age_days": age,
                       **{k: (None if v is None else ("Yes" if v else "No")) for k, v in flags.items()},
                       "flags": sum(1 for v in flags.values() if v)})

    def count(key):
        return sum(1 for c in checks if c[key] == "Yes")

    # Issues: a check whose source field is absent is reported as unavailable, never as zero.
    issues, gaps = [], []
    def issue(key, title, orders, severity, action, available=True, order_ids=None):
        if not available:
            gaps.append(title)
            return
        if orders:
            issues.append({"id": key, "issue": title, "orders": orders, "severity": severity, "action": action,
                           "order_ids": order_ids or []})

    ids = lambda key: [c["order_id"] for c in checks if c[key] == "Yes"]
    issue("delivered_date_but_open", "Delivery date on file but status still open", count("delivered_date"), "High",
          "Re-sync status from courier tracking and close these as delivered.", present["delivery_date"], ids("delivered_date"))
    remark_rows = [r for r in rows if r["courier_remark"] and re.search(r"\bdelivered\b", r["courier_remark"], re.I)
                   and categorize_status(r["status"]) != "delivered"]
    issue("courier_remark_delivered", "Courier remark says delivered", len(remark_rows), "High",
          "Remark and status disagree: check the status mapping in the tracking feed.", present["courier_remark"],
          [r["order_id"] for r in remark_rows])
    issue("cancel_note_on_active", "Cancellation note on an active shipment", count("cancel_note"), "High",
          "Confirm with the courier; stop or raise RTO, and claim back charges if not moved.",
          present["cancel_remark"] or present["cancel_reason"], ids("cancel_note"))
    issue("open_over_30_days", f"Open more than {OVER_DAYS} days", count("over_30_days"), "Medium",
          "Escalate the AWB list to each courier and check for loss.", present["order_date"], ids("over_30_days"))
    pickups = {r["pickup_date"] for r in rows if r["pickup_date"]}
    pickup_action = ("All share " + _fmt_day(next(iter(pickups))) + ", likely a placeholder value: fix at source."
                     if len(pickups) == 1 and count("pickup_before_order") > 1 else "Correct the pickup date at source.")
    issue("pickup_before_order", "Pickup date earlier than order date", count("pickup_before_order"), "Medium",
          pickup_action, present["pickup_date"], ids("pickup_before_order"))
    outliers = [r for r, c in zip(rows, checks) if c["charge_outlier"] == "Yes"]
    outlier_action = "Audit this invoice before payment."
    if len(outliers) == 1 and median_charge:
        o = outliers[0]
        ratio = (o["shipping_charge"] / median_charge).quantize(Decimal("1"))
        outlier_action = (f"Audit this invoice before payment; {_num(o['weight_kg'])} kg costing {ratio}× the median charge."
                          if o["weight_kg"] else f"Audit this invoice before payment; {ratio}× the median charge.")
    issue("charge_outlier", f"Shipping charge over {OUTLIER_FACTOR}× the median", count("charge_outlier"), "Medium",
          outlier_action, bool(charges), ids("charge_outlier"))
    tests = [r for r in rows if r["product"] and re.match(r"test", r["product"], re.I)]
    issue("test_orders", "Test orders (product named test)", len(tests), "Low",
          "Exclude test orders from business reports.", present["product"], [r["order_id"] for r in tests])

    # Cost by courier.
    by: dict[str, dict[str, Any]] = {}
    for r in rows:
        c = by.setdefault(r["courier"], {"courier": r["courier"], "orders": 0, "weight": Decimal(0), "charges": Decimal(0), "weighed": 0})
        c["orders"] += 1
        if r["weight_kg"] is not None:
            c["weight"] += r["weight_kg"]; c["weighed"] += 1
        if r["shipping_charge"] is not None:
            c["charges"] += r["shipping_charge"]
    couriers = [{"courier": c["courier"], "orders": c["orders"], "weight_kg": _num(c["weight"]), "charges": _r2(c["charges"]),
                 "avg_charge": _r2(c["charges"] / c["orders"]),
                 "charge_per_kg": _r2(c["charges"] / c["weight"]) if c["weight"] > 0 else None}
                for c in sorted(by.values(), key=lambda c: -c["orders"])]

    mix, all_orders = account_status(container.get("account_summary"))
    spend = sum(charges, Decimal(0))
    goods = [r["invoice_value"] for r in rows if r["invoice_value"] is not None]
    ages = [c["age_days"] for c in checks if c["age_days"] is not None]
    kpis = {
        "orders": n,
        "all_orders": all_orders,
        "share_of_all": round(n / all_orders, 4) if all_orders else None,
        "flagged": sum(1 for c in checks if c["flags"] > 0),
        "delivered_but_open": count("delivered_date") if present["delivery_date"] else None,
        "cancel_noted": count("cancel_note") if (present["cancel_remark"] or present["cancel_reason"]) else None,
        "median_age_days": _median_upper(ages),
        "spend": _r2(spend) if charges else None,
        "avg_charge": _r2(spend / len(charges)) if charges else None,
        "median_charge": _r2(median_charge) if median_charge is not None else None,
        "goods_value": _num(sum(goods, Decimal(0))) if goods else None,
    }

    statuses = {categorize_status(r["status"]) for r in rows}
    single_status = len(statuses) == 1 and n > 0
    label = report_filter.get("status_label") or (rows[0]["status"] if single_status and rows[0]["status"] else "All")
    dates = sorted(r["order_date"] for r in rows if r["order_date"])
    period_from = report_filter.get("from_date") or (dates[0] if dates else None)
    period_to = report_filter.get("to_date") or (dates[-1] if dates else None)
    title = f"{label} shipments" + (f", {_fmt_day(period_from)} – {_fmt_day(period_to)}" if period_from and period_to else "")

    # Deterministic takeaway (numbers are code-generated, never by the AI).
    parts = []
    if kpis["delivered_but_open"]:
        parts.append(f"{kpis['delivered_but_open']} of the {n} shipments already have a delivery date")
    if kpis["cancel_noted"]:
        parts.append(f"{kpis['cancel_noted']} carry a cancellation note")
    if kpis["median_age_days"] is not None:
        parts.append(f"half have been open for at least {kpis['median_age_days']} days")
    takeaway = (", ".join(parts[:-1]) + (", and " if len(parts) > 1 else "") + parts[-1] + "." if parts else
                f"{n} shipments analysed.")
    if any(i["severity"] == "High" for i in issues):
        takeaway += " The courier status feed needs fixing before this list is used for follow-ups or delivery KPIs."
    takeaway = takeaway[0].upper() + takeaway[1:]

    detail = [{
        "order_id": r["order_id"], "order_date": r["order_date"], "courier": r["courier"], "dest_city": r["dest_city"],
        "status": r["status"], "payment_mode": r["payment_mode"],
        "weight_kg": _num(r["weight_kg"]) if r["weight_kg"] is not None else None,
        "shipping_charge": _r2(r["shipping_charge"]) if r["shipping_charge"] is not None else None,
        "invoice_value": _num(r["invoice_value"]) if r["invoice_value"] is not None else None,
        "eway_bill": None if r["eway_bill"] is None else ("Yes" if r["eway_bill"] else "No"),
        "delivery_date": r["delivery_date"], "age_days": c["age_days"], "flags": c["flags"],
    } for r, c in zip(rows, checks)]

    return {
        "schema_version": SCHEMA_VERSION,
        "title": title,
        "takeaway": takeaway,
        "as_of": as_of.isoformat(),
        "scope": {"orders": n, "status_label": label, "single_status": single_status,
                  "from_date": period_from, "to_date": period_to,
                  "account_rates_note": "Rates such as delivery rate are shown at account level; the filtered rows hold one status."
                  if single_status else None},
        "kpis": kpis,
        "issues": issues,
        "data_gaps": [f"Check not run, field missing in the data: {g}" for g in gaps],
        "account_status_mix": mix,
        "couriers": couriers,
        "order_checks": checks,
        "orders": detail,
        "charts": {
            "account_status": {"type": "bar", "highlight": label, "rows": mix},
            "age_by_order": {"type": "bar", "threshold": OVER_DAYS,
                             "rows": [{"order_id": c["order_id"], "value": c["age_days"]} for c in checks if c["age_days"] is not None]},
            "charge_by_order": {"type": "bar", "reference": kpis["median_charge"],
                                "rows": [{"order_id": r["order_id"], "value": _r2(r["shipping_charge"]),
                                          "outlier": c["charge_outlier"] == "Yes"}
                                         for r, c in zip(rows, checks) if r["shipping_charge"] is not None]},
        },
        "theme": THEME,
    }


def insights_narrative(insights: dict[str, Any]) -> dict[str, Any]:
    """Code-written narrative used when the AI text cannot be verified (no 502 for the user).

    Every number comes from `insights`, which Python calculated.
    """
    severity = {"High": "high", "Medium": "medium", "Low": "low"}
    priority = {"High": "high", "Medium": "medium", "Low": "low"}
    issues = insights["issues"]
    return {
        "title": insights["title"][:200],
        "executive_summary": {"heading": "Executive Summary", "paragraphs": [insights["takeaway"][:1200]], "highlights": []},
        "sections": [],
        "findings": [{"title": i["issue"][:250], "detail": f"{i['orders']} of {insights['kpis']['orders']} shipments.",
                      "severity": severity[i["severity"]]} for i in issues][:20],
        "recommendations": [{"action": i["action"][:600], "rationale": i["issue"][:3000], "priority": priority[i["severity"]]}
                            for i in issues][:15],
        "data_limitations": (insights["data_gaps"] + ([insights["scope"]["account_rates_note"]]
                             if insights["scope"].get("account_rates_note") else []))[:20],
    }


def _inr(value: float | int, decimals: int = 2) -> str:
    """Indian digit grouping, e.g. 617802 -> ₹6,17,802; 12304.4 -> ₹12,304.40."""
    whole, _, frac = f"{float(value):.{decimals}f}".partition(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:]); head = head[:-2]
    if head:
        groups.insert(0, head)
    text = ",".join(groups + [tail]) if groups else tail
    return "₹" + text + (f".{frac}" if decimals else "")


def headline_cards(insights: dict[str, Any]) -> list[dict[str, Any]]:
    """The dashboard's KPI cards (label, display value, note), all from Python-calculated insights."""
    k, scope = insights["kpis"], insights["scope"]
    is_single = scope.get("single_status")
    status_label = scope.get("status_label") or "All"

    order_note = f"{k['share_of_all'] * 100:.1f}% of {k['all_orders']:,} orders on the account" if k.get("all_orders") else (f"Status: {status_label}" if is_single else None)

    cards = [{
        "key": "orders",
        "label": f"Shipments ({status_label})" if is_single and status_label != "All" else "Total Shipments",
        "value": f"{k['orders']:,}",
        "raw": k["orders"],
        "unit": "number",
        "note": order_note,
        "icon": "box",
        "tone": "normal",
    }]

    if k.get("flagged", 0) > 0:
        cards.append({
            "key": "flagged",
            "label": "Flagged Issues",
            "value": f"{k['flagged']:,}",
            "raw": k["flagged"],
            "unit": "number",
            "note": "Tracking / data issues detected",
            "icon": "alert",
            "tone": "bad",
        })
    elif is_single and any(term in str(status_label).lower() for term in ("rto", "cancel", "undelivered", "exception", "lost")):
        cards.append({
            "key": "status_cohort",
            "label": "Cohort Status",
            "value": status_label,
            "raw": k["orders"],
            "unit": "text",
            "note": "100% of analyzed cohort",
            "icon": "alert",
            "tone": "warning",
        })
    elif is_single and "delivered" in str(status_label).lower():
        cards.append({
            "key": "delivery_rate",
            "label": "Delivery Rate",
            "value": "100%",
            "raw": 100,
            "unit": "percent",
            "note": "All analyzed orders delivered",
            "icon": "check",
            "tone": "ok",
        })
    else:
        cards.append({
            "key": "flagged",
            "label": "Data Quality",
            "value": "100%",
            "raw": 0,
            "unit": "text",
            "note": "No tracking anomalies",
            "icon": "check",
            "tone": "ok",
        })

    if k.get("spend") is not None:
        cards.append({
            "key": "spend",
            "label": "Shipping Spend",
            "value": _inr(k["spend"]),
            "raw": k["spend"],
            "unit": "text",
            "note": "Total freight charges",
            "icon": "currency",
            "tone": "normal",
        })
        cards.append({
            "key": "avg_charge",
            "label": "Avg. Charge / Order",
            "value": _inr(k["avg_charge"]),
            "raw": k["avg_charge"],
            "unit": "text",
            "note": f"Median {_inr(k['median_charge'])}" if k.get("median_charge") else "Average shipping cost",
            "icon": "trending",
            "tone": "normal",
        })
    if k.get("goods_value") is not None:
        cards.append({
            "key": "goods_value",
            "label": "Goods Value",
            "value": _inr(k["goods_value"], 0),
            "raw": k["goods_value"],
            "unit": "text",
            "note": "Sum of invoice values",
            "icon": "currency",
            "tone": "normal",
        })
    return cards
