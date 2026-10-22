"""Validate authoritative employee, seller, membership and ERP evidence mappings."""

from __future__ import annotations
import re
from ..errors import EscalationError
from ..utils.ist import date_filter

STATUSES = [
    "Delivered",
    "Manifested",
    "Cancelled",
    "RTO Delivered",
    "Undelivered",
    "RTO In Transit",
    "In-Transit",
    "Pickup Scheduled",
    "Out for Delivery",
    "Not Picked",
    "Lost",
]
LIMITS = dict(employees=500, sellers=2000, groups=500, members=5000, shipments=20000, activity=15500)


def validate_mappings(source: dict | None = None) -> dict:
    source = {} if source is None else source

    def bad(message: str) -> None:
        raise EscalationError(400, message)

    if not isinstance(source, dict):
        bad("Mappings must be a JSON object")
    result = {}
    for key, limit in LIMITS.items():
        rows = source.get(key, [])
        if not isinstance(rows, list) or len(rows) > limit or any(not isinstance(row, dict) for row in rows):
            bad("Invalid " + key + " mapping size")
        result[key] = [dict(row) for row in rows]

    def string(row: dict, key: str, required: bool = False) -> None:
        if key not in row and not required:
            return
        if not isinstance(row.get(key), str) or len(row[key]) > 250 or (required and not row[key].strip()):
            bad("Invalid mapping " + key)

    def unique(rows: list[dict], keys: tuple) -> None:
        values = [tuple(row.get(k, "") for k in keys) for row in rows]
        if len(set(values)) != len(values):
            bad("Duplicate mapping identity")

    for kind in ("employees", "sellers"):
        for row in result[kind]:
            for key in ("id", "name"):
                string(row, key, True)
            for key in ("company", "department", "status", "location", "salesPoc"):
                string(row, key)
        unique(result[kind], ("id",))
    ids = {kind: {row["id"] for row in result[kind]} for kind in ("employees", "sellers")}

    def references(row: dict) -> None:
        for key, kind in (("employeeId", "employees"), ("sellerId", "sellers")):
            string(row, key)
            if row.get(key) and row[key] not in ids[kind]:
                bad("Mapping references an unknown " + key)

    for row in result["groups"]:
        string(row, "groupId", True)
        references(row)
        if not row.get("employeeId") and not row.get("sellerId"):
            bad("Group mapping needs an employee or seller")
    unique(result["groups"], ("groupId",))
    for row in result["members"]:
        string(row, "senderId", True)
        string(row, "groupId")
        references(row)
        if bool(row.get("employeeId")) == bool(row.get("sellerId")):
            bad("Member needs exactly one employee or seller identity")
    unique(result["members"], ("groupId", "senderId"))
    for row in result["shipments"]:
        string(row, "groupId", True)
        string(row, "awb", True)
        string(row, "opsRemark")
        if not re.fullmatch(r"\d{8,16}", row["awb"], re.ASCII) or row.get("status") not in STATUSES:
            bad("Invalid AWB or ERP status")
        for key in (
            "observedAt",
            "manifestedAt",
            "pickupAt",
            "deliveredAt",
            "eddAt",
            "responseMessageId",
            "closureMessageId",
        ):
            if key not in row and key != "observedAt":
                continue
            if type(row.get(key)) is not int or not 0 < row[key] < 2**53:
                bad("Invalid evidence timestamp or reference " + key)
        if any(row.get(k, 0) > row["observedAt"] for k in ("manifestedAt", "pickupAt", "deliveredAt")):
            bad("Historical status date cannot be after the ERP observation")
        if row.get("pickupAt") and row.get("deliveredAt") and row["deliveredAt"] < row["pickupAt"]:
            bad("Delivery cannot precede pickup")
    unique(result["shipments"], ("groupId", "awb", "observedAt"))
    for row in result["activity"]:
        string(row, "groupId", True)
        if not row.get("date"):
            bad("Activity needs a real date")
        date_filter(row["date"], row["date"])
        if type(row.get("active")) is not bool:
            bad("Activity must supply an explicit boolean")
    unique(result["activity"], ("groupId", "date"))
    return result
