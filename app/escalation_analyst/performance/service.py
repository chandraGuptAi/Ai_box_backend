"""Period resolution, frozen source generation and consistent filtering across all outputs."""

from __future__ import annotations
from datetime import date, timedelta
import time
import hashlib
import json
from .engine import build_performance, assemble
from .mappings import validate_mappings
from .options import category_of
from ..utils.ist import day, date_filter
from ..errors import EscalationError


def period_window(options: dict, now: int | None = None) -> dict:
    now = int(time.time()) if now is None else now
    today = date.fromisoformat(day(now))
    period = options.get("period", "today")
    if period == "custom":
        start, end = options.get("fromDate"), options.get("toDate")
    elif period == "previous":
        last = today.replace(day=1) - timedelta(days=1)
        start, end = last.replace(day=1).isoformat(), last.isoformat()
    else:
        end = (today - timedelta(days=1) if period == "yesterday" else today).isoformat()
        start = (
            today.replace(day=1).isoformat()
            if period == "mtd"
            else (today - timedelta(days=6)).isoformat() if period == "7" else end
        )
    window = date_filter(start, end)
    if not window:
        raise EscalationError(400, "Choose a report period")
    if period == "last24":
        window = {"from": now - 86400, "to": now}
        start, end = day(window["from"]), day(now - 1)
    if window["to"] - window["from"] > 366 * 86400:
        raise EscalationError(400, "Select at most 366 days")
    return {**window, "fromDate": start, "toDate": end}


def generate(runtime, options: dict) -> dict:
    request = {**options, **period_window(options)}
    request["mappings"] = validate_mappings(options.get("mappings"))
    groups = runtime.groups.list()
    ids = request.get("groupIds") or [g["group_id"] for g in groups]
    unknown = set(ids) - {g["group_id"] for g in groups}
    if unknown:
        raise EscalationError(400, "Unknown group selected")
    request.update(groupIds=ids, groupNames={g["group_id"]: g["group_name"] for g in groups})
    messages = runtime.groups.messages(None, request, ids)
    if len(messages) > 100000:
        raise EscalationError(400, "Source limits exceeded")
    result = filtered(build_performance(messages, request, request["mappings"]), request, options)
    result["coverage"]["fingerprint"] = fingerprint(messages)
    result["meta"] = dict(
        builderVersion="2.0.0",
        calculationVersion="1.0.0",
        generatedAt=int(time.time()),
        coverage=result["coverage"],
    )
    return {"snapshot": result, "request": request}


def fingerprint(messages: list[dict]) -> str:
    rows = [{k: m.get(k) for k in ("id", "timestamp", "group_id", "body", "author")} for m in messages]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def filtered(result: dict, request: dict, filters: dict) -> dict:
    def values(key):
        value = filters.get(key, [])
        return value if isinstance(value, list) else [value] if value else []

    def any_value(allowed, *candidates):
        return not allowed or any(c in allowed for c in candidates)

    groups = values("groupIds") + values("groupId")
    employees = values("employeeIds") + values("employeeId")
    sellers = values("sellerIds") + values("sellerId")
    statuses = values("statuses") + values("status")
    observed = values("observed")
    categories = values("categories")
    miles = values("miles") + values("mile")
    if not any(
        (
            groups,
            employees,
            sellers,
            statuses,
            observed,
            categories,
            miles,
            filters.get("awb"),
            filters.get("method"),
        )
    ):
        return result
    records = [
        r
        for r in result["records"]
        if any_value(groups, r["groupId"])
        and any_value(employees, r["employeeId"], r["assignedEmployeeId"])
        and any_value(sellers, r["sellerId"], r["assignedSellerId"])
        and any_value(statuses, (r["erp"] or {}).get("status"))
        and any_value(observed, r["status"])
        and any_value(categories, *[category_of(o["category"]) for o in r["observations"]])
        and any_value(miles, r["mile"], *[o["mile"] for o in r["observations"]])
        and (not filters.get("awb") or filters["awb"] in r["awb"])
        and (not filters.get("method") or filters["method"] == r["processingMethod"])
    ]
    selected = {r["groupId"] for r in records}
    mappings = {
        **request["mappings"],
        "groups": [
            g
            for g in request["mappings"]["groups"]
            if any_value(groups, g["groupId"])
            and any_value(employees, g.get("employeeId"))
            and any_value(sellers, g.get("sellerId"))
            and (
                not any((statuses, observed, categories, miles, filters.get("awb"), filters.get("method")))
                or g["groupId"] in selected
            )
        ],
    }
    source = {
        (r["groupId"], m["id"]): {**m, "group_id": r["groupId"], "group_name": r["groupName"]}
        for r in records
        for m in r["sourceMessages"]
    }
    normalized = {
        **result,
        "records": records,
        "summary": {
            **result["summary"],
            "messagesProcessed": len(source),
            "uniqueAwbs": len({r["awb"] for r in records}),
            "extractedEntries": sum(r["entryCount"] for r in records),
            "recordCount": len(records),
        },
    }
    group_scope = (
        groups if groups else [g["groupId"] for g in mappings["groups"]] if employees or sellers else None
    )
    assembled = assemble(
        normalized,
        {**request, "groupIds": group_scope if group_scope is not None else request.get("groupIds", [])},
        mappings,
        list(source.values()),
    )
    return {
        **assembled,
        "groupActivity": [
            g for g in result.get("groupActivity", []) if group_scope is None or g["groupId"] in group_scope
        ],
        "coverage": result.get("coverage"),
        "meta": result.get("meta"),
    }
