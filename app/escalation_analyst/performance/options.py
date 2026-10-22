"""Selectable report sections, classifications and grouping modes."""

from __future__ import annotations

SECTIONS = [
    ["summary", "Executive summary"],
    ["kpis", "KPI cards"],
    ["status", "Status distribution"],
    ["trend", "Daily & MTD performance"],
    ["kam", "KAM performance"],
    ["seller", "Seller performance"],
    ["classification", "Escalation classification"],
    ["tat", "TAT & resolution"],
    ["activity", "Active / inactive groups"],
    ["awbs", "AWB-wise records"],
    ["timelines", "Conversation timelines"],
    ["ai", "AI findings & recommendations"],
]
MILES = ["First Mile", "In-Transit", "Last Mile", "Unclassified"]
OBSERVED = [
    "Out For Delivery",
    "In Transit",
    "Undelivered (Generic)",
    "Re-dispatch Required",
    "Customer Contact Issue",
    "Shipment Missing",
    "RTO Escalation",
    "Cancellation Request",
    "Delivered",
    "Unclassified",
]
GROUP_BY = ["none", "group", "date", "kam", "seller", "mile", "category", "observed", "erp"]
MODES = dict(
    summary=["summary", "kpis", "status", "trend", "classification", "tat", "ai"],
    detailed=[k for k, _ in SECTIONS if k != "timelines"],
    conversation=[k for k, _ in SECTIONS],
)
CATEGORIES = [
    "Pickup Pending",
    "Urgent Pickup Request",
    "Shipping Related \u2013 Urgent Delivery (Delay & Misroute)",
    "High Value Shipment Delay",
    "RTO Escalation",
    "RTO Revoke / Fake Remark / Self Collect",
    "Cancellation Request",
    "POD / Delivery Proof Request",
    "Customer Contact / NDR Issue",
    "Delivered / Delivery Confirmation",
]
OTHER_CATEGORY = "Other / unclassified"


def category_of(value: str) -> str:
    return value if value in CATEGORIES else OTHER_CATEGORY
