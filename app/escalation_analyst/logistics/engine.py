"""AWB evidence aggregation; stable daily/history record identities."""

from __future__ import annotations

import json
import math
import re
from .awb import extract_awbs
from .classifier import classify
from .tat import calculate_tat, duration
from ..errors import EscalationError
from ..utils.ist import day


def build_report(messages: list[dict], request: dict) -> dict:
    mode, view = request.get("mode", "enhanced"), request.get("view", "history")
    legacy = mode == "legacy"
    quality = dict.fromkeys(
        (
            "uncertainCandidates",
            "unclassifiedStatus",
            "unverifiedResponse",
            "missingClosure",
            "invalidTimestamps",
            "skippedRecords",
        ),
        0,
    )
    seen, ordered = set(), []
    for m in messages:
        ts = m.get("timestamp")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not math.isfinite(ts):
            quality["invalidTimestamps"] += 1
            quality["skippedRecords"] += 1
            continue
        key = (m["group_id"], m["id"])
        if not request["from"] <= ts < request["to"] or key in seen:
            continue
        seen.add(key)
        ordered.append(m)
    ordered.sort(key=lambda m: (m["group_id"], m["timestamp"], m["id"]))
    next_other = {}
    following = None
    for index in range(len(ordered) - 1, -1, -1):
        current = ordered[index]
        after = ordered[index + 1] if index + 1 < len(ordered) else None
        if not after or after["group_id"] != current["group_id"]:
            following = None
        elif (after.get("author") or after.get("author_name")) != (
            current.get("author") or current.get("author_name")
        ):
            following = after
        next_other[current["id"]] = following
    records = {}
    evidence_bytes = 0
    for m in ordered:
        body = m.get("body") or ""
        candidates = extract_awbs(body, legacy)
        evidence_bytes += len(body.encode("utf-8")) * len(candidates)
        if len(candidates) > 100 or evidence_bytes > 64 * 1024 * 1024:
            raise EscalationError(400, "Evidence size limit exceeded")
        for candidate in candidates:
            key = json.dumps(
                [m["group_id"], day(m["timestamp"]) if view == "daily" else "", candidate["awb"]],
                separators=(",", ":"),
                ensure_ascii=False,
            )
            if key not in records:
                records[key] = dict(
                    key=key,
                    awb=candidate["awb"],
                    groupId=m["group_id"],
                    groupName=m.get("group_name") or m["group_id"],
                    date=day(m["timestamp"]),
                    firstAt=m["timestamp"],
                    lastAt=m["timestamp"],
                    sourceMessages=[],
                    observations=[],
                    entryCount=0,
                    confidence=candidate["confidence"],
                    mode=mode,
                )
            r = records[key]
            if len(records) > 20000:
                raise EscalationError(400, "Record limit exceeded")
            lines = re.split(r"\r?\n", body)
            relevant = [line for line in lines if re.search(r"\b" + candidate["awb"] + r"\b", line, re.ASCII)]
            context = [
                line
                for line in lines
                if not any(re.search(r"\b" + c["awb"] + r"\b", line, re.ASCII) for c in candidates)
            ]
            classified = classify(
                (relevant[-1] if relevant else body) if legacy else "\n".join(relevant + context), legacy
            )
            r["entryCount"] += max(1, len(relevant)) if legacy else 1
            r.update(lastAt=m["timestamp"], sender=m.get("author_name") or m.get("author"))
            if candidate["confidence"] == "explicit":
                r["confidence"] = "explicit"
            r["sourceMessages"].append(
                {k: m.get(k) for k in ("id", "author", "author_name", "timestamp", "body", "msg_type")}
            )
            r["observations"].append(
                dict(messageId=m["id"], timestamp=m["timestamp"], **classified, extraction=candidate)
            )
            r.update({k: classified[k] for k in ("status", "mile", "category")})
            other = next_other[m["id"]]
            r.update(
                legacyResponseSeconds=duration(m["timestamp"], other["timestamp"]) if other else None,
                legacyResponseAt=other["timestamp"] if other else None,
            )
    output = list(records.values())
    for r in output:
        r.update(calculate_tat(r["sourceMessages"][0], r["sourceMessages"]))
        quality["uncertainCandidates"] += r["confidence"] == "candidate"
        quality["unclassifiedStatus"] += r["status"] == "Unclassified"
        quality["unverifiedResponse"] += r["responseSeconds"] is None
        quality["missingClosure"] += r["resolutionSeconds"] is None
    output.sort(key=lambda r: (r["firstAt"], r["groupId"], r["awb"]))
    return dict(
        engineVersion="1.0.0",
        records=output,
        summary=dict(
            messagesProcessed=len(ordered),
            uniqueAwbs=len({r["awb"] for r in output}),
            groupAwbs=len({(r["groupId"], r["awb"]) for r in output}),
            extractedEntries=sum(r["entryCount"] for r in output),
            recordCount=len(output),
            confirmedResponses=sum(r["responseSeconds"] is not None for r in output),
        ),
        quality=quality,
    )
