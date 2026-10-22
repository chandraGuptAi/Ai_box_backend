"""Ten performance sheets derived from one frozen evidence-aware daily dataset."""

from __future__ import annotations
from datetime import date
import json
from pathlib import Path
from .mappings import STATUSES, validate_mappings
from .normalize import normalize, summarize, buckets
from .entities import entity_sheets, activity_sheets
from ..utils.ist import format_time, day

SHEETS = [
    "Overview",
    "Day_Wise_Performance",
    "KAM_MTD_Performance",
    "KAM_Daily_Performance",
    "Seller_MTD_Performance",
    "Seller_Daily_Performance",
    "KAM_Active_Inactive_Escalation",
    "Active_Inactive_Seller_Esca..",
    "AI_MTD_Performance",
    "Backup",
]
REFERENCE = json.loads((Path(__file__).parent / "reference-workbook-spec.json").read_text(encoding="utf-8"))


def headers(name: str) -> list:
    return [h["value"] for s in REFERENCE["sheets"] if s["name"] == name for h in s.get("headers", [])]


def build_performance(messages: list[dict], request: dict, mappings: dict | None = None) -> dict:
    mappings = validate_mappings(mappings)
    normalized = normalize(messages, request, mappings)
    source = [m for m in messages if request["from"] <= m["timestamp"] < request["to"]]
    result = assemble(normalized, request, mappings, source)
    activity = {
        group_id: dict(
            groupId=group_id,
            groupName=request.get("groupNames", {}).get(group_id, group_id),
            messages=0,
            lastAt=None,
        )
        for group_id in request.get("groupIds", [])
    }
    for m in source:
        row = activity.setdefault(
            m["group_id"],
            dict(
                groupId=m["group_id"], groupName=m.get("group_name") or m["group_id"], messages=0, lastAt=None
            ),
        )
        row["messages"] += 1
        row["lastAt"] = max(row["lastAt"] or 0, m["timestamp"])
        row["groupName"] = m.get("group_name") or row["groupName"]
    result["groupActivity"] = sorted(activity.values(), key=lambda r: (-r["messages"], r["groupName"]))
    result["coverage"] = dict(
        messages=len(source),
        groups=len({m["group_id"] for m in source}),
        firstAt=min((m["timestamp"] for m in source), default=None),
        lastAt=max((m["timestamp"] for m in source), default=None),
        maxMessageId=max((m["id"] for m in source), default=None),
    )
    return result


def assemble(normalized: dict, request: dict, mappings: dict, messages: list[dict]) -> dict:
    records = normalized["records"]
    summary = summarize(records)
    sheets = {}
    notes = [
        "Captured retained messages only; AWB mentions are candidates, not confirmed incidents.",
        "ERP statuses use the latest imported observation before the reporting cutoff, not historical status at each message. Message classifications are separate.",
        "AWB/LR count sums daily group/AWB records; distinct AWBs is shown separately.",
        "Entity attribution uses the last AWB mention author, matching the reference; group ownership alone is not authorship.",
        "AI/manual counts unavailable: existing AI reports lack a normalized AWB provenance link.",
    ]

    def make(name, columns, rows, note="", formats=None):
        sheets[name] = dict(name=name, columns=columns, rows=rows, note=note, formats=formats or {})

    overview = [
        ["Reporting start", request["fromDate"]],
        ["Reporting end", request["toDate"]],
        ["Timezone", "Asia/Kolkata"],
        ["Calculation version", "1.0.0"],
        ["Captured messages", normalized["summary"]["messagesProcessed"]],
        ["Groups with AWB mentions", len({r["groupId"] for r in records})],
        ["Groups in scope", len(request.get("groupIds", {m["group_id"] for m in messages}))],
        ["Raw AWB entries", summary["entries"]],
        ["Daily AWB records", summary["records"]],
        ["Distinct AWBs", summary["distinctAwbs"]],
        ["Verified responses", summary["verifiedResponses"]],
        ["ERP statuses available", summary["known"]],
        ["ERP statuses unavailable", summary["unknown"]],
        ["Employee-attributed records", sum(bool(r["employeeId"]) for r in records)],
        ["Seller-attributed records", sum(bool(r["sellerId"]) for r in records)],
        ["First captured source", format_time(min(m["timestamp"] for m in messages)) if messages else None],
        ["Last captured source", format_time(max(m["timestamp"] for m in messages)) if messages else None],
        [
            "Completeness",
            (
                "Incomplete upstream data"
                if summary["unknown"] or not mappings["groups"]
                else "Available imported evidence; source coverage still limited"
            ),
        ],
    ] + [["Data note " + str(i + 1), n] for i, n in enumerate(notes)]
    make("Overview", ["Metric", "Value"], overview)
    daily = []
    for rows in buckets(records, lambda r: (r["date"], r["groupId"])):
        r = rows[0]
        s = summarize(rows)
        daily.append(
            [
                r["date"],
                r["groupName"],
                r["groupId"],
                "Deterministic",
                s["entries"],
                s["records"],
                s["distinctAwbs"],
                s["unknown"],
                *[
                    sum(row["mile"] == mile for row in rows)
                    for mile in ("First Mile", "Last Mile", "In-Transit", "Unclassified")
                ],
                *[s["counts"][status] if s["known"] else None for status in STATUSES],
            ]
        )
    make(
        "Day_Wise_Performance",
        [
            "Date",
            "Group Name",
            "Group ID",
            "Processing Method",
            "No. of Escalation Entries",
            "AWB / LR Escalation",
            "Distinct AWBs",
            "ERP Status unavailable",
            "First Mile",
            "Last Mile",
            "In-Transit",
            "Unclassified stage",
            *STATUSES,
        ],
        daily,
        notes[2],
        {0: "date"},
    )
    entity_sheets(records, mappings, make, headers)
    activity_sheets(records, mappings, request, messages, make, headers)
    make(
        "AI_MTD_Performance",
        ["Metric", "Count", "Definition"],
        [
            ["Raw escalation entries", summary["entries"], "AWB mentions in captured messages"],
            ["Daily AWB records", summary["records"], "Daily group/AWB records"],
            ["Deterministic processed records", summary["records"], "Not AI processing"],
            ["Manually processed records", None, "No validated manual provenance"],
            ["Automated/AI processed records", None, "No normalized AI-to-AWB source link"],
            *[
                [status, summary["counts"][status] if summary["known"] else None, "Imported ERP status only"]
                for status in STATUSES
            ],
            ["ERP Status unavailable", summary["unknown"], "No imported dated shipment evidence"],
        ],
        "Processing methods are distinct; overlapping counts are not summed.",
    )
    make(
        "Backup",
        headers("Backup")
        + [
            "Source Group ID",
            "Source Message IDs",
            "Observed message status",
            "AWB confidence",
            "Mapped Employee ID",
            "Mapped Seller ID",
            "ERP status observed at",
            "Verified response message ID",
            "Verified closure message ID",
        ],
        backup_rows(records, mappings),
        "Original A:Y layout preserved including unlabeled X (AWB length) and Y (blank). Extra columns preserve traceability; ERP and observed statuses remain separate.",
        {0: "date", 13: "date", 14: "date", 16: "date", 17: "date", 18: "date", 20: "date"},
    )
    return dict(
        version="1.0.0",
        sheets={name: sheets[name] for name in SHEETS},
        summary={
            **normalized["summary"],
            "confirmedResponses": summary["verifiedResponses"],
            "erpKnown": summary["known"],
            "erpUnknown": summary["unknown"],
            "employeeRecords": sum(bool(r["employeeId"]) for r in records),
            "sellerRecords": sum(bool(r["sellerId"]) for r in records),
        },
        quality={
            **normalized["quality"],
            "unverifiedResponse": sum(r["responseSeconds"] is None for r in records),
            "missingClosure": sum(r["resolutionSeconds"] is None for r in records),
            "notes": notes,
            "coverage": "Captured retained messages only; unavailable history cannot be reconstructed.",
        },
        records=records,
    )


def backup_rows(records: list[dict], mappings: dict) -> list[list]:
    employees = {m["id"]: m for m in mappings["employees"]}
    sellers = {m["id"]: m for m in mappings["sellers"]}

    def stamp(ts):
        return format_time(ts) if ts is not None else None

    def date_value(ts):
        return day(ts) if ts is not None else None

    def duration(value):
        return f"{value//3600}h {value%3600//60}m" if value is not None else None

    rows = []
    for r in records:
        erp = r["erp"] or {}
        master = employees.get(r["employeeId"]) if r["employeeId"] else sellers.get(r["sellerId"])
        identity = (
            master["name"]
            + "-"
            + master["id"]
            + "-"
            + (master.get("company", "BIGSHIP") if r["employeeId"] else "Seller")
            if master
            else None
        )
        elapsed = (
            (date.fromisoformat(day(erp["deliveredAt"])) - date.fromisoformat(day(erp["pickupAt"]))).days
            if erp.get("deliveredAt") and erp.get("pickupAt")
            else None
        )
        tat = (
            ("IN TAT" if day(erp["deliveredAt"]) <= day(erp["eddAt"]) else "OUT TAT")
            if erp.get("eddAt") and erp.get("deliveredAt")
            else None
        )
        rows.append(
            [
                r["date"],
                r["groupName"],
                r["processingMethod"],
                identity,
                r["entryCount"],
                r["awb"],
                r["mile"],
                r["category"],
                erp.get("status"),
                stamp(r["firstAt"]),
                stamp(r["closureAt"]),
                duration(r["responseSeconds"]),
                duration(r["resolutionSeconds"]),
                date_value(r["responseAt"]),
                date_value(r["closureAt"]),
                r["responseSeconds"] / 60 if r["responseSeconds"] is not None else None,
                date_value(erp.get("manifestedAt")),
                date_value(erp.get("pickupAt")),
                date_value(erp.get("deliveredAt")),
                elapsed,
                date_value(erp.get("eddAt")),
                tat,
                erp.get("opsRemark"),
                len(r["awb"]),
                None,
                r["groupId"],
                ", ".join(str(m["id"]) for m in r["sourceMessages"]),
                r["status"],
                r["confidence"],
                r["employeeId"],
                r["sellerId"],
                stamp(erp.get("observedAt")),
                r["responseMessageId"],
                r["closureMessageId"],
            ]
        )
    return rows
