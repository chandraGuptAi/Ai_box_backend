"""Deterministic status, mile and category precedence from the existing Node engine."""

from __future__ import annotations

import re
from ..utils.js_compat import utf16_slice


def classify(text: str, legacy: bool = False) -> dict:
    text = str(text or "")
    t = text.lower()

    def has(*words: str) -> bool:
        return any(word in t for word in words)

    def matches(pattern: str) -> bool:
        return bool(re.search(pattern, t, re.ASCII))

    neg_delivery = has(
        "undeliver",
        "not deliver",
        "deliver ni",
        "deliver nahi",
        "deliver nhi",
        "delivery nahi",
        "delivered nahi",
        "delivered nhi",
        "not yet delivered",
        "not been delivered",
        "nahi hua",
        "nhi hua",
        "nahi hui",
        "nhi hui",
    )
    neg_cancel = has(
        "cancel ni",
        "cancel nahi",
        "not cancel",
        "cancelled ni",
        "cancelled nahi",
        "do not cancel",
        "don't cancel",
    )
    neg_rto = has("rto ni", "rto nahi", "not rto")
    status, evidence = "Unclassified", None
    rules = [
        (legacy and "deliver" in t and not neg_delivery, "Delivered", "legacy deliver substring"),
        ("rto" in t and not neg_rto, "RTO Escalation", "rto"),
        ("cancel" in t and not neg_cancel, "Cancellation Request", "cancel"),
        (
            not legacy
            and matches(r"not (?:yet )?(?:out for delivery|ofd)|(?:ofd|out for delivery)\s+(?:nahi|nhi|ni)"),
            "Undelivered (Generic)",
            "negated OFD",
        ),
        (matches(r"\bout for delivery\b|\bofd\b"), "Out For Delivery", "out for delivery / OFD"),
        (neg_delivery, "Undelivered (Generic)", "negated delivery"),
        (has("in transit", "in-transit", "transit"), "In Transit", "transit"),
        (
            has("re-dispatch", "reattempt", "re dispatch", "hold"),
            "Re-dispatch Required",
            "re-dispatch / reattempt / hold",
        ),
        (has("missing", "lost"), "Shipment Missing", "missing / lost"),
        (
            has("call", "msg", "appointment", "contact", "reachable"),
            "Customer Contact Issue",
            "contact keyword",
        ),
        (
            not legacy
            and matches(r"\bdelivered\b|delivery (?:done|complete)|deliver ho (?:gaya|gyi|gayi)")
            and not matches(r"\b(?:please|pls|request|will|should|kab|when|not|nahi|nhi)\b"),
            "Delivered",
            "reported delivery completion (unverified)",
        ),
        (legacy, "Undelivered (Generic)", "legacy fallback"),
    ]
    for condition, value, rule in rules:
        if condition:
            status, evidence = value, rule
            break
    mile = "Unclassified"
    if has("pickup", "pick up", "dispatch pending", "manifest", "pur"):
        mile = "First Mile"
    elif has(
        "transit",
        "in-transit",
        "movement",
        "departed",
        "arrived",
        "bag",
        "hub",
        "facility",
        "stuck",
        "mid mile",
    ):
        mile = "In-Transit"
    elif (
        legacy
        or status
        in (
            "Delivered",
            "Out For Delivery",
            "Undelivered (Generic)",
            "RTO Escalation",
            "Re-dispatch Required",
            "Customer Contact Issue",
        )
        or matches(r"\b(?:ndr|pod|delivery)\b")
    ):
        mile = "Last Mile"
    categories = [
        (has("fake", "self collect", "rto revoke"), "RTO Revoke / Fake Remark / Self Collect"),
        (has("high value", "12k", "17k", "25k", "66k"), "High Value Shipment Delay"),
        (
            "pickup" in t,
            (
                ("Pickup Pending (3 Days)" if legacy else "Pickup Pending")
                if "pending" in t
                else "Urgent Pickup Request"
            ),
        ),
        (
            has("misroute", "urgent delivery", "delay"),
            "Shipping Related \u2013 Urgent Delivery (Delay & Misroute)",
        ),
        ("rto" in t, "RTO Escalation"),
        ("cancel" in t, "Cancellation Request"),
        ("pod" in t, "POD / Delivery Proof Request"),
        (has("ndr", "unavail", "call", "msg"), "Customer Contact / NDR Issue"),
        (status == "Delivered", "Delivered / Delivery Confirmation"),
    ]
    category = next((value for condition, value in categories if condition), None)
    if category is None:
        cleaned = text.strip()
        category = (
            cleaned if len(cleaned.encode("utf-16-le")) // 2 <= 50 else utf16_slice(cleaned, 47) + "..."
        )
    return dict(status=status, mile=mile, category=category, evidence=evidence, closureVerified=False)
