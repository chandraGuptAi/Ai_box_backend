"""Validate a /api/reports/generate response against the report v3 reference (11 In-Transit orders).

Usage (from E:\\Office\\AI_Box_backend):
    python scripts/validate_report_v3.py new_report_v3.json
Prints a PASS/FAIL table and exits with code 1 if any check fails.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def get(obj, path, default=None):
    for part in re.findall(r"[^.\[\]]+", path):
        try:
            obj = obj[int(part)] if isinstance(obj, list) else obj[part]
        except (KeyError, IndexError, TypeError, ValueError):
            return default
    return obj


def main(path: str) -> int:
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    ins = report.get("insights") or {}
    issues = {i.get("id"): i for i in ins.get("issues", [])}
    couriers = {c.get("courier"): c for c in ins.get("couriers", [])}
    text = json.dumps(report, ensure_ascii=False)
    checks = [
        ("insights block present", bool(ins), True),
        ("schema_version", ins.get("schema_version"), "3.0"),
        ("theme.brand.primary", get(ins, "theme.brand.primary"), "#6C4CF1"),
        ("kpis.orders", get(ins, "kpis.orders"), 11),
        ("kpis.all_orders", get(ins, "kpis.all_orders"), 64),
        ("kpis.share_of_all", get(ins, "kpis.share_of_all"), 0.1719),
        ("kpis.spend", get(ins, "kpis.spend"), 12304.4),
        ("kpis.avg_charge", get(ins, "kpis.avg_charge"), 1118.58),
        ("kpis.median_charge", get(ins, "kpis.median_charge"), 508.94),
        ("kpis.goods_value", get(ins, "kpis.goods_value"), 617802),
        ("kpis.median_age_days (as_of 2026-10-13)", get(ins, "kpis.median_age_days"), 55),
        ("kpis.flagged", get(ins, "kpis.flagged"), 11),
        ("kpis.delivered_but_open", get(ins, "kpis.delivered_but_open"), 6),
        ("kpis.cancel_noted", get(ins, "kpis.cancel_noted"), 3),
        ("account mix: Delivered 35 / 54.7%", [get(ins, "account_status_mix[0].orders"), get(ins, "account_status_mix[0].share")], [35, 0.5469]),
        ("issue delivered_date_but_open", get(issues, "delivered_date_but_open.orders"), 6),
        ("issue courier_remark_delivered", get(issues, "courier_remark_delivered.orders"), 11),
        ("issue cancel_note_on_active", get(issues, "cancel_note_on_active.orders"), 3),
        ("issue open_over_30_days", get(issues, "open_over_30_days.orders"), 10),
        ("issue pickup_before_order", get(issues, "pickup_before_order.orders"), 11),
        ("issue charge_outlier order_ids", get(issues, "charge_outlier.order_ids"), ["252742834"]),
        ("issue test_orders", get(issues, "test_orders.orders"), 10),
        ("Delhivery charge_per_kg", get(couriers, "LTL Delhivery.charge_per_kg"), 215.88),
        ("Ekart charge_per_kg", get(couriers, "LTL Ekart.charge_per_kg"), 39.58),
        ("TCI charge_per_kg", get(couriers, "LTL TCI Express.charge_per_kg"), 46.73),
        ("detail rows", len(ins.get("orders", [])), 11),
        ("charts present", sorted((ins.get("charts") or {}).keys()), ["account_status", "age_by_order", "charge_by_order"]),
        ("no 'Delivery Rate' KPI for the filtered set",
         any("rate" in str(m.get("label", "")).lower() for m in report.get("key_metrics", [])), False),
        ("no 'Delivered Shipments 0' card on the filtered set",
         any(m.get("label") == "Delivered Shipments" and str(m.get("value")) == "0" for m in report.get("key_metrics", [])), False),
        ("KPI cards", [c.get("value") for c in ins.get("kpi_cards", [])], ["11", "11", "₹12,304.40", "₹1,118.58", "₹6,17,802"]),
        ("no unresolved '{{' anywhere", "{{" in text, False),
        ("no internal field names in narrative", bool(re.search(r"status_category_breakdown|derived_percentages|shipping_financial",
                                                               json.dumps(report.get("content", {})))), False),
        ("v2 fields kept (content, structured_report)", bool(report.get("content")) and bool(report.get("structured_report")), True),
        ("meta.fallback_used (informational)", get(ins, "meta.fallback_used"), None),
    ]
    failed = 0
    print(f"{'Check':48} {'Result':6} Value")
    for name, value, expected in checks:
        if expected is None:
            print(f"{name:48} {'INFO':6} {value}")
            continue
        ok = value == expected
        failed += not ok
        print(f"{name:48} {'PASS' if ok else 'FAIL':6} {value}" + ("" if ok else f"   (expected {expected})"))
    print(f"\n{len(checks) - 1 - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "new_report_v3.json"))
