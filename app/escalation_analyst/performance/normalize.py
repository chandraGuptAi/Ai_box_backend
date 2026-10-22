"""Frozen daily AWB dataset and explicitly verified response/closure links."""

from __future__ import annotations
from collections import defaultdict
from datetime import date, timedelta
from ..logistics.engine import build_report
from ..logistics.awb import extract_awbs
from ..utils.ist import day
from .mappings import STATUSES


def dates(request: dict) -> list[str]:
    start = date.fromisoformat(day(request["from"]))
    end = date.fromisoformat(day(request["to"] - 1))
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def buckets(rows: list[dict], key) -> list[list[dict]]:
    grouped = defaultdict(list)
    for row in rows:
        identity = key(row)
        if identity is not None:
            grouped[identity].append(row)
    return list(grouped.values())


def summarize(rows: list[dict]) -> dict:
    counts = dict.fromkeys(STATUSES, 0)
    for row in rows:
        if row.get("erp"):
            counts[row["erp"]["status"]] += 1
    known = sum(counts.values())
    times = [row["responseSeconds"] / 60 for row in rows if row["responseSeconds"] is not None]
    return dict(
        entries=sum(r["entryCount"] for r in rows),
        records=len(rows),
        distinctAwbs=len({r["awb"] for r in rows}),
        counts=counts,
        known=known,
        unknown=len(rows) - known,
        avg=sum(times) / len(times) if times else None,
        verifiedResponses=len(times),
        delivered=counts["Delivered"] if known == len(rows) else None,
        pending=len(rows) - counts["Delivered"] if known == len(rows) else None,
    )


def member(mappings: dict, group_id: str, author: str) -> dict:
    return next(
        (row for row in mappings["members"] if row.get("groupId") == group_id and row["senderId"] == author),
        None,
    ) or next(
        (row for row in mappings["members"] if not row.get("groupId") and row["senderId"] == author), {}
    )


def normalize(messages: list[dict], request: dict, mappings: dict) -> dict:
    result = build_report(messages, {**request, "mode": "enhanced", "view": "daily"})
    employees = {e["id"]: e for e in mappings["employees"]}
    sellers = {s["id"]: s for s in mappings["sellers"]}
    groups = {g["groupId"]: g for g in mappings["groups"]}
    shipments = {}
    for row in mappings["shipments"]:
        key = (row["groupId"], row["awb"])
        if row["observedAt"] < request["to"] and (
            key not in shipments or row["observedAt"] > shipments[key]["observedAt"]
        ):
            shipments[key] = row
    by_id = {m["id"]: m for m in messages}
    for record in result["records"]:
        identity = member(mappings, record["groupId"], record["sourceMessages"][-1].get("author"))
        assignment = groups.get(record["groupId"], {})
        eid, sid = identity.get("employeeId"), identity.get("sellerId")
        erp = shipments.get((record["groupId"], record["awb"]))
        record.update(
            employeeId=eid,
            sellerId=sid,
            assignedEmployeeId=assignment.get("employeeId"),
            assignedSellerId=assignment.get("sellerId"),
            employeeName=employees.get(eid, {}).get("name"),
            sellerName=sellers.get(sid, {}).get("name"),
            processingMethod="Deterministic",
            erp=erp,
        )

        def evidence(key: str) -> dict | None:
            row = by_id.get((erp or {}).get(key))
            if (
                row
                and row["group_id"] == record["groupId"]
                and record["firstAt"] <= row["timestamp"] < request["to"]
                and any(c["awb"] == record["awb"] for c in extract_awbs(row.get("body", "")))
                and member(mappings, row["group_id"], row.get("author")).get("employeeId")
                and row.get("author") != record["sourceMessages"][0].get("author")
            ):
                return row
            return None

        response, closure = evidence("responseMessageId"), evidence("closureMessageId")
        record.update(
            responseSeconds=response["timestamp"] - record["firstAt"] if response else None,
            responseAt=response["timestamp"] if response else None,
            responseMessageId=response["id"] if response else None,
            resolutionSeconds=closure["timestamp"] - record["firstAt"] if closure else None,
            closureAt=closure["timestamp"] if closure else None,
            closureMessageId=closure["id"] if closure else None,
        )
    return result
