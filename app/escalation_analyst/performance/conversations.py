"""Merge daily AWB records and expose source-linked conversation timelines."""

from __future__ import annotations
from .normalize import buckets, member
from .options import CATEGORIES, OTHER_CATEGORY
from ..utils.ist import day


def conversations(result: dict, mappings: dict) -> list[dict]:
    employees = {r["id"]: r["name"] for r in mappings["employees"]}
    sellers = {r["id"]: r["name"] for r in mappings["sellers"]}
    output = []
    for rows in buckets(result["records"], lambda r: (r["groupId"], r["awb"])):
        rows = sorted(rows, key=lambda r: r["firstAt"])
        last = rows[-1]
        by_id = {}
        for row in rows:
            for m, o in zip(row["sourceMessages"], row["observations"]):
                by_id.setdefault(m["id"], {**m, "observation": o})
        messages = sorted(by_id.values(), key=lambda m: (m["timestamp"], m["id"]))
        first = messages[0]
        opener = first.get("author") or first.get("author_name")
        reply = next(
            (
                m
                for m in messages
                if m["id"] != first["id"]
                and m["timestamp"] >= first["timestamp"]
                and (m.get("author") or m.get("author_name"))
                and (m.get("author") or m.get("author_name")) != opener
                and not member(mappings, last["groupId"], m.get("author")).get("sellerId")
            ),
            None,
        )
        erp = next((r["erp"] for r in rows if r.get("erp")), None)
        verified = next((r for r in rows if r["responseAt"] is not None), None)
        closed = next((r for r in rows if r["closureAt"] is not None), None)
        eid = next((r["employeeId"] for r in reversed(rows) if r.get("employeeId")), None) or next(
            (r["assignedEmployeeId"] for r in rows if r.get("assignedEmployeeId")), None
        )
        sid = next((r["sellerId"] for r in reversed(rows) if r.get("sellerId")), None) or next(
            (r["assignedSellerId"] for r in rows if r.get("assignedSellerId")), None
        )
        mile = next(
            (
                m["observation"]["mile"]
                for m in reversed(messages)
                if m["observation"]["mile"] != "Unclassified"
            ),
            "Unclassified",
        )
        category = next(
            (m["observation"]["category"] for m in messages if m["observation"]["category"] in CATEGORIES),
            OTHER_CATEGORY,
        )
        output.append(
            dict(
                key=last["groupId"] + "|" + last["awb"],
                groupId=last["groupId"],
                groupName=last["groupName"],
                awb=last["awb"],
                confidence="explicit" if any(r["confidence"] == "explicit" for r in rows) else "candidate",
                firstAt=first["timestamp"],
                lastAt=messages[-1]["timestamp"],
                date=day(first["timestamp"]),
                days=len(rows),
                entries=sum(r["entryCount"] for r in rows),
                messageCount=len(messages),
                mile=mile,
                category=category,
                rawCategory=last["category"],
                observed=last["status"],
                erpStatus=erp["status"] if erp else None,
                erp=erp,
                linkedReplySeconds=reply["timestamp"] - first["timestamp"] if reply else None,
                linkedReplyId=reply["id"] if reply else None,
                verifiedResponseSeconds=verified["responseAt"] - first["timestamp"] if verified else None,
                verifiedResponseId=verified["responseMessageId"] if verified else None,
                resolutionSeconds=closed["closureAt"] - first["timestamp"] if closed else None,
                closureId=closed["closureMessageId"] if closed else None,
                deliverySeconds=(
                    erp["deliveredAt"] - first["timestamp"]
                    if erp and erp.get("deliveredAt", 0) >= first["timestamp"]
                    else None
                ),
                employeeId=eid,
                sellerId=sid,
                kam=employees.get(eid),
                seller=sellers.get(sid),
                messages=messages,
            )
        )
    return output


def public_conversation(row: dict) -> dict:
    return {key: value for key, value in row.items() if key not in ("messages", "erp")}


def timeline(result: dict, request: dict, group_id: str, awb: str) -> dict | None:
    row = next(
        (
            r
            for r in conversations(result, request["mappings"])
            if r["groupId"] == group_id and r["awb"] == awb
        ),
        None,
    )
    if not row:
        return None
    events = []
    for m in row["messages"]:
        who = member(request["mappings"], group_id, m.get("author"))
        role = "KAM" if who.get("employeeId") else "Seller" if who.get("sellerId") else "Unmapped"
        marks = []
        for key, label in [
            ("linkedReplyId", "First linked reply"),
            ("verifiedResponseId", "Verified response"),
            ("closureId", "Verified closure"),
        ]:
            if m["id"] == row[key]:
                marks.append(label)
        if m["id"] == row["messages"][0]["id"]:
            marks.insert(0, "First report")
        events.append(
            dict(
                id=m["id"],
                timestamp=m["timestamp"],
                sender=m.get("author_name") or m.get("author") or "Unknown sender",
                role=role,
                mappedName=row["kam"] if role == "KAM" else row["seller"] if role == "Seller" else None,
                body=m.get("body") or "",
                type=m.get("msg_type") or "chat",
                action=" / ".join(marks + [m["observation"]["status"]]),
            )
        )
    return {
        **public_conversation(row),
        "erp": row["erp"],
        "events": events,
        "note": "Only messages in this group that mention this AWB are linked; message mentions do not verify ERP delivery.",
    }
