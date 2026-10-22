"""Original employee/seller column layouts and imported daily activity calculations."""

from __future__ import annotations
from datetime import date
from .normalize import buckets, summarize, dates
from .mappings import STATUSES


def entity_sheets(records: list[dict], mappings: dict, make, headers) -> None:
    for kind in ("employees", "sellers"):
        kam = kind == "employees"
        id_key = "employeeId" if kam else "sellerId"
        masters = {m["id"]: m for m in mappings[kind]}
        for daily in (False, True):
            name = ("KAM" if kam else "Seller") + ("_Daily_Performance" if daily else "_MTD_Performance")
            entities = [
                dict(
                    id=rows[0][id_key],
                    date=rows[0]["date"] if daily else None,
                    master=masters[rows[0][id_key]],
                    **summarize(rows),
                )
                for rows in buckets(
                    records, lambda r: (r["date"] if daily else "", r[id_key]) if r.get(id_key) else None
                )
            ]
            metric = "delivered" if kam else "records"
            for entity in entities:
                entity["rank"] = (
                    None
                    if entity[metric] is None
                    else 1
                    + sum(other[metric] is not None and other[metric] > entity[metric] for other in entities)
                )
            output = []
            for index, e in enumerate(entities):
                m = e["master"]
                identity = (
                    m["name"] + "-" + e["id"] + "-" + (m.get("company", "BIGSHIP") if kam else "Seller")
                )
                counts = [e["counts"][status] if e["known"] else None for status in STATUSES]
                if daily:
                    row = [
                        identity,
                        index + 1,
                        e["date"],
                        date.fromisoformat(e["date"]).strftime("%A"),
                        e["id"],
                        m["name"],
                    ]
                    row += [m.get("company"), m.get("department")] if kam else ["Seller"]
                    row += [
                        e["entries"],
                        e["records"],
                        *counts,
                        e["known"] or None,
                        e["avg"],
                        None,
                        e["unknown"] == 0,
                        e["unknown"],
                    ]
                elif kam:
                    row = [
                        identity,
                        index + 1,
                        e["id"],
                        m["name"],
                        m.get("company"),
                        m.get("department"),
                        e["entries"],
                        e["records"],
                        e["delivered"],
                        e["pending"],
                        e["avg"],
                        e["delivered"] / e["records"] - 1 if e["delivered"] is not None else None,
                        e["rank"],
                        None,
                        *counts,
                        e["known"] or None,
                        None,
                        e["unknown"] == 0,
                        e["unknown"],
                    ]
                else:
                    row = [
                        identity,
                        index + 1,
                        e["id"],
                        m["name"],
                        "Seller",
                        e["entries"],
                        e["records"],
                        e["avg"],
                        e["records"] / e["entries"] - 1 if e["entries"] else 0,
                        e["rank"],
                        None,
                        *counts,
                        e["known"] or None,
                        None,
                        e["unknown"] == 0,
                        e["unknown"],
                    ]
                output.append(row + [e["distinctAwbs"], e["unknown"], e["verifiedResponses"]])
            formats = {2: "date"} if daily else {11 if kam else 8: "percent"}
            make(
                name,
                headers(name) + ["Distinct AWBs", "ERP Status unavailable", "Verified response count"],
                output,
                "Verified author mappings and dated ERP evidence only; missing values remain unavailable.",
                formats,
            )


def activity_sheets(
    records: list[dict], mappings: dict, request: dict, messages: list[dict], make, headers
) -> None:
    calendar = dates(request)
    sellers = {r["id"]: r for r in mappings["sellers"]}
    employees = {r["id"]: r for r in mappings["employees"]}
    activity = {(r["groupId"], r["date"]): r["active"] for r in mappings["activity"]}
    groups = [
        g for g in mappings["groups"] if "groupIds" not in request or g["groupId"] in request["groupIds"]
    ]
    names = {m["group_id"]: m.get("group_name") or m["group_id"] for m in messages}
    counts = {}
    for r in records:
        key = (r["groupId"], r["date"])
        counts[key] = counts.get(key, 0) + r["entryCount"]
    output = []
    for index, g in enumerate(groups):
        seller = sellers.get(g.get("sellerId"), {})
        employee = employees.get(g.get("employeeId"), {})
        flags = [activity.get((g["groupId"], d)) for d in calendar]
        total = sum(flags) if all(f is not None for f in flags) else None
        identity = lambda m, suffix: m["name"] + "-" + m["id"] + "-" + suffix if m else None
        entries = [counts.get((g["groupId"], d), 0) for d in calendar]
        output.append(
            [
                identity(seller, "Seller"),
                identity(employee, employee.get("company", "BIGSHIP")),
                index + 1,
                g.get("sellerId"),
                seller.get("name"),
                names.get(g["groupId"], g["groupId"]),
                seller.get("name"),
                "Seller" if seller else None,
                g.get("employeeId"),
                employee.get("name"),
                employee.get("company"),
                seller.get("salesPoc"),
                seller.get("location"),
                ("Active" if total > 0.05 else "In-Active") if total is not None else None,
                total,
                *[int(f) if f is not None else None for f in flags],
                total,
                None,
                *entries,
                sum(entries),
                g["groupId"],
                seller.get("status"),
            ]
        )
    make(
        "Active_Inactive_Seller_Esca..",
        [
            "Seller identity",
            "Employee identity",
            "S.No",
            "SELLER CUSTOMER ID",
            "SELLER COMPANY NAME",
            "Actual Seller Company Name",
            "SELLER NAME",
            "Type",
            "EMP ID",
            "ASSIGNED",
            "COMPANY",
            "SALE POC NAME",
            "LOCATION",
            "Active / In_Active",
            "MTD Active",
            *calendar,
            "Selected period total",
            None,
            *calendar,
            "Captured escalation entries",
            "Group ID",
            "Actual management status",
        ],
        output,
        "Activity requires explicit daily flags; unavailable flags do not imply inactivity.",
    )
    output = []
    for index, e in enumerate(mappings["employees"]):
        own = [g for g in groups if g.get("employeeId") == e["id"]]
        if not own:
            continue
        available = all((g["groupId"], d) in activity for g in own for d in calendar)
        active = (
            sum(any(activity.get((g["groupId"], d)) for d in calendar) for g in own) if available else None
        )
        count = len(own)
        daily = [
            (
                sum(bool(activity.get((g["groupId"], d))) for g in own)
                if all((g["groupId"], d) in activity for g in own)
                else None
            )
            for d in calendar
        ]
        output.append(
            [
                e["name"] + "-" + e["id"] + "-" + e.get("company", "BIGSHIP"),
                index + 1,
                e["id"],
                e["name"],
                e.get("company"),
                e.get("department"),
                count,
                active,
                count - active if active is not None else None,
                active / count if active is not None else None,
                (count - active) / count if active is not None else None,
                *daily,
            ]
        )
    make(
        "KAM_Active_Inactive_Escalation",
        headers("KAM_Active_Inactive_Escalation")[:11] + calendar,
        output,
        "Assigned master groups; imported daily activity is independent of WhatsApp connection.",
        {9: "percent", 10: "percent"},
    )
