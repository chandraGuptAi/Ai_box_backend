"""Report preview aggregates derived only from the frozen performance snapshot."""

from __future__ import annotations
from .normalize import buckets, dates, summarize
from .mappings import STATUSES
from .options import SECTIONS, MILES, OBSERVED, GROUP_BY, MODES, CATEGORIES, OTHER_CATEGORY, category_of
from .statistics import stats
from .conversations import conversations, public_conversation, timeline


def build_details(result: dict, request: dict, group_by: str = "none") -> dict:
    rows = conversations(result, request["mappings"])
    known = [r for r in rows if r["erpStatus"]]
    kpis = dict(
        messages=result["summary"]["messagesProcessed"],
        entries=result["summary"]["extractedEntries"],
        records=result["summary"]["recordCount"],
        distinctAwbs=result["summary"]["uniqueAwbs"],
        conversations=len(rows),
        groups=len({r["groupId"] for r in rows}),
        explicitAwbs=sum(r["confidence"] == "explicit" for r in rows),
        candidateAwbs=sum(r["confidence"] != "explicit" for r in rows),
        erpKnown=len(known),
        erpUnavailable=len(rows) - len(known),
        delivered=sum(r["erpStatus"] == "Delivered" for r in known) if known else None,
        cancelled=sum(r["erpStatus"] == "Cancelled" for r in known) if known else None,
        rto=sum(r["erpStatus"] in ("RTO Delivered", "RTO In Transit") for r in known) if known else None,
        open=(
            sum(
                r["erpStatus"] not in ("Delivered", "Cancelled", "Lost", "RTO Delivered", "RTO In Transit")
                for r in known
            )
            if known
            else None
        ),
        reportedDeliveredUnverified=sum(
            any(m["observation"]["status"] == "Delivered" for m in r["messages"]) for r in rows
        ),
        linkedReplies=sum(r["linkedReplySeconds"] is not None for r in rows),
        noReply=sum(r["linkedReplySeconds"] is None for r in rows),
        medianReplySeconds=stats([r["linkedReplySeconds"] for r in rows])["median"],
        verifiedResponses=sum(r["verifiedResponseSeconds"] is not None for r in rows),
        verifiedClosures=sum(r["resolutionSeconds"] is not None for r in rows),
    )

    def counts(values, order=()):
        result = dict.fromkeys(order, 0)
        for value in values:
            result[value] = result.get(value, 0) + 1
        return [dict(label=key, count=value) for key, value in result.items()]

    tat = {
        key: stats([r[field] for r in rows])
        for key, field in [
            ("linkedReply", "linkedReplySeconds"),
            ("verifiedResponse", "verifiedResponseSeconds"),
            ("verifiedResolution", "resolutionSeconds"),
            ("erpDelivery", "deliverySeconds"),
        ]
    }
    breakdown = []
    field = {
        "group": "groupName",
        "date": "date",
        "kam": "kam",
        "seller": "seller",
        "mile": "mile",
        "category": "category",
        "observed": "observed",
        "erp": "erpStatus",
    }.get(group_by)
    if field:
        for grouped in buckets(rows, lambda r: r["groupId"] if group_by == "group" else r[field]):
            breakdown.append(
                dict(
                    label=grouped[0][field],
                    conversations=len(grouped),
                    entries=sum(r["entries"] for r in grouped),
                    distinctAwbs=len({r["awb"] for r in grouped}),
                    linkedReplies=sum(r["linkedReplySeconds"] is not None for r in grouped),
                    medianReplySeconds=stats([r["linkedReplySeconds"] for r in grouped])["median"],
                    erpDelivered=sum(r["erpStatus"] == "Delivered" for r in grouped),
                    erpUnavailable=sum(not r["erpStatus"] for r in grouped),
                )
            )

    def entity(kind):
        key = "employeeId" if kind == "employees" else "sellerId"
        masters = {e["id"]: e for e in request["mappings"][kind]}
        output = []
        for grouped in buckets(result["records"], lambda r: r.get(key)):
            eid = grouped[0][key]
            summary = summarize(grouped)
            own = [r for r in rows if r[key] == eid]
            reply = stats([r["linkedReplySeconds"] for r in own])
            output.append(
                dict(
                    id=eid,
                    name=masters[eid]["name"],
                    assignedGroups=sum(g.get(key) == eid for g in request["mappings"]["groups"]),
                    entries=summary["entries"],
                    records=summary["records"],
                    distinctAwbs=summary["distinctAwbs"],
                    conversations=len(own),
                    linkedReplies=reply["count"],
                    medianReplySeconds=reply["median"],
                    verifiedResponses=summary["verifiedResponses"],
                    avgVerifiedResponseMinutes=summary["avg"],
                    erpKnown=summary["known"],
                    delivered=summary["delivered"],
                    pending=summary["pending"],
                )
            )
        return sorted(output, key=lambda r: (-r["records"], r["name"]))

    trend = []
    cumulative = 0
    for d in dates(request):
        daily = [r for r in result["records"] if r["date"] == d]
        cumulative += len(daily)
        trend.append(
            dict(
                date=d,
                entries=sum(r["entryCount"] for r in daily),
                records=len(daily),
                distinctAwbs=len({r["awb"] for r in daily}),
                newConversations=sum(r["date"] == d for r in rows),
                linkedReplies=sum(r["date"] == d and r["linkedReplySeconds"] is not None for r in rows),
                cumulativeRecords=cumulative,
            )
        )
    return dict(
        builderVersion="2.0.0",
        calculationVersion=result["version"],
        meta=result.get("meta"),
        freshness=None,
        request={
            **{k: request.get(k) for k in ("fromDate", "toDate", "from", "to", "period")},
            "groupCount": len(request.get("groupIds", [])),
        },
        summary=[
            f"{len(rows)} group/AWB conversations from {kpis['messages']} captured messages. ERP and observed message classifications are separate."
        ],
        kpis=kpis,
        status=dict(
            erp=(
                [dict(label=s, count=sum(r["erpStatus"] == s for r in known)) for s in STATUSES]
                if known
                else []
            ),
            erpUnavailable=kpis["erpUnavailable"],
            observed=counts((r["observed"] for r in rows), OBSERVED),
        ),
        trend=trend,
        classification=dict(
            categories=[
                c for c in counts((r["category"] for r in rows), CATEGORIES + [OTHER_CATEGORY]) if c["count"]
            ],
            miles=counts((r["mile"] for r in rows), MILES),
        ),
        tat=tat,
        kam=entity("employees"),
        seller=entity("sellers"),
        activity={"groups": result.get("groupActivity", []), "kam": []},
        breakdown=breakdown,
        groupBy=group_by,
        ai=None,
        recommendations=[
            dict(
                level="info",
                text="Review unresolved conversations. Import dated ERP evidence before claiming delivery or verified closure.",
            )
        ],
        quality=result["quality"],
    )
