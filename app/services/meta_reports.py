"""WhatsApp Business / Instagram / Facebook Page reports from synced Meta metrics (``data_sources``).

Every figure comes from Ai_Box_Service ``/api/meta/report-context``, which reads what its background sync
stored from Meta. Nothing is estimated or filled in:
- a requested source with no connected account -> MetaNotConnectedError (action: connect);
- expired/revoked authorization -> MetaReconnectRequiredError (action: reconnect);
- connected but nothing synced for the dates -> MetaNoDataError, or a section that says so when other
  sources do have data;
- stale data -> reported with a visible freshness note.

Values from different sources/accounts are never added together. The optional AI observations get only
aliased sources (no account names or IDs) and are kept only if every number they quote is one of the
supplied values.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any

from app.errors import (AIPrivacyError, InvalidPromptError, MetaNoDataError, MetaNotConnectedError,
                        MetaReconnectRequiredError)
from app.models.report_schema import (BulletBlock, ChartAxis, ChartDataset, DisplayMetric, DisplaySection, KeyMetric,
                                      ParagraphBlock, ReportChart, ReportContent, ReportFinding, ReportResponse,
                                      ReportTable, StructuredReport, StructuredSummary, TableBlock, TableColumn)
from app.services.meta_source import MetaSource

# A question that names a Meta channel and nothing shipment-related is answered from social data only.
_SOCIAL_WORDS = re.compile(r"\b(?:whats\s?app|wa|insta(?:gram)?|ig|face\s?book|fb|meta|reels?|social)\b"
                           r"|व्हाट्सएप|व्हाट्सऐप|इंस्टाग्राम|इंस्टा|फेसबुक", re.IGNORECASE)
_SHIPMENT_WORDS = re.compile(r"\b(?:shipments?|orders?|couriers?|deliver(?:y|ies|ed)?|rto|ndr|cod|awb|parcels?|"
                             r"pick-?ups?|logistics|freight|remittances?|zones?|pin\s?codes?|dispatch(?:ed)?|"
                             r"returns?|tracking|warehouses?|carriers?)\b"
                             r"|शिपमेंट|ऑर्डर|आर्डर|डिलीवरी|कूरियर|पार्सल", re.IGNORECASE)


def social_only_prompt(prompt: str | None) -> bool:
    text = prompt or ""
    return bool(_SOCIAL_WORDS.search(text)) and not _SHIPMENT_WORDS.search(text)

PLATFORM_LABELS = {"whatsapp": "WhatsApp Business", "instagram": "Instagram", "facebook": "Facebook Page"}
METRIC_LABELS = {
    "messages_sent": "Messages sent", "messages_delivered": "Messages delivered",
    "pricing_volume": "Billable messages", "template_sent": "Template messages sent",
    "template_delivered": "Template messages delivered", "template_read": "Template messages read",
    "template_clicked": "Template button clicks", "status_read": "Messages read (webhooks)",
    "status_failed": "Messages failed (webhooks)",
    "reach": "Accounts reached", "views": "Views", "accounts_engaged": "Accounts engaged",
    "total_interactions": "Total interactions", "likes": "Likes", "comments": "Comments", "shares": "Shares",
    "saves": "Saves", "replies": "Replies", "profile_links_taps": "Profile link taps", "follows": "New follows",
    "unfollows": "Unfollows", "followers_count": "Followers", "media_count": "Published posts",
    "page_media_view": "Content views", "page_total_media_view_unique": "Unique viewers",
    "page_post_engagements": "Post engagements", "page_views_total": "Page views",
    "page_daily_follows_unique": "New follows", "page_daily_unfollows_unique": "Unfollows",
    "page_follows": "Followers", "post_media_view": "Post views", "post_total_media_view_unique": "Post unique viewers",
    "post_clicks": "Post clicks", "post_reactions": "Post reactions",
}
# Headline metrics, in order of preference (additive daily totals or snapshots only).
HEADLINES = {
    "whatsapp": ("messages_sent", "messages_delivered", "status_read"),
    "instagram": ("views", "total_interactions", "followers_count"),
    "facebook": ("page_media_view", "page_post_engagements", "page_views_total"),
}
REASONS = {
    "permission_required": "permission not granted to BigShip",
    "unsupported": "not provided by Meta for this account",
    "not_synced": "not synced yet",
    "expired_token": "the account must be reconnected",
    "not_available": "Meta returned no data for these dates",
}
MAX_ACCOUNTS_PER_PLATFORM = 3
MAX_TABLES = 15
_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def metric_label(name: str) -> str:
    if name in METRIC_LABELS:
        return METRIC_LABELS[name]
    if name.startswith("media_"):
        return "Post " + name[6:].replace("_", " ")
    return name.replace("_", " ").capitalize()


def fmt(value: Any) -> str:
    if value is None:
        return "Not available"
    if isinstance(value, (int, float)):
        return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"
    return str(value)


def _day(value: Any) -> str:
    text = str(value or "")
    return text[:10] if text else "unknown"


def _text(value: Any, limit: int) -> str:
    return str(value).replace("<", "‹").replace(">", "›")[:limit]


class MetaReports:
    def __init__(self, ai, source: MetaSource | None = None) -> None:
        self.ai = ai
        self.source = source or MetaSource()

    async def generate(self, request, reports, user_id: str | None, credential: str | None) -> ReportResponse:
        from app.services.shipment_source import delegated_credential
        credential = delegated_credential(credential)   # the seller's own login; Ai_Box_Service verifies whose
        filters = request.filters
        sources = request.meta_sources
        context = await self.source.context(sources, filters.from_date, filters.to_date, filters.timezone,
                                            credential, user_id)
        external, notes = None, []
        with_shipments = "shipments" in (request.data_sources or [])
        if with_shipments and social_only_prompt(request.prompt):
            # Shipments card selected, but the question is only about WhatsApp/Instagram/Facebook.
            with_shipments = False
            notes.append("Shipments are not included because the question is only about WhatsApp, Instagram or "
                         "Facebook. Mention orders or shipments in the question to add them.")
        self._check(sources, context, with_shipments)

        if with_shipments:
            shipment_request = request.model_copy(update={"data_sources": None})
            try:
                external = await reports._generate_report(shipment_request, shipment_credential=credential,
                                                          user_id=user_id)
            except InvalidPromptError as error:
                if error.code == "clarification_required":
                    raise
                # The prompt is about social data only; report shipments with the standard analysis.
                from app.prompts.registry import get_report_definition
                default = get_report_definition("shipment").default_prompt
                external = await reports._generate_report(shipment_request.model_copy(update={"prompt": default}),
                                                          shipment_credential=credential, user_id=user_id)
                notes.append("The shipment part of this report uses the standard shipment analysis, because the "
                             "question was about WhatsApp, Instagram or Facebook data.")

        report, facts = self.build(request, context, external, notes)
        findings, ai_calls = await self._observations(request.prompt, facts, context)
        if findings:
            report.content.findings = findings
            report.structured_report.findings = findings
        report.generation["ai_calls"] += ai_calls

        from app.analysis.accuracy import digest, seal
        from app.errors import request_id_var
        seal(report, SimpleNamespace(fingerprint=digest(json.loads(json.dumps(context, default=str))),
                                     request_id=request_id_var.get() or uuid.uuid4().hex))
        return report

    # ---- status gate -----------------------------------------------------------------------------

    @staticmethod
    def _check(sources: list[str], context: dict[str, Any], with_shipments: bool) -> None:
        status = {p: (context["sources"].get(p) or {}).get("status") for p in sources}
        missing = [p for p in sources if status[p] in (None, "not_connected")]
        if missing:
            names = ", ".join(PLATFORM_LABELS[p] for p in missing)
            raise MetaNotConnectedError(f"Connect your {names} account to include it in this report.",
                                        details={"platforms": missing, "action": "connect"})
        reconnect = [p for p in sources if status[p] == "reconnect_required"]
        if reconnect:
            names = ", ".join(PLATFORM_LABELS[p] for p in reconnect)
            raise MetaReconnectRequiredError(f"Reconnect your {names} account: its Meta authorization expired "
                                             "or was removed.", details={"platforms": reconnect, "action": "reconnect"})
        empty = [p for p in sources if status[p] == "no_data"]
        if len(empty) == len(sources) and not with_shipments:
            reasons = {p: _empty_reason(context["sources"][p]) for p in empty}
            names = ", ".join(PLATFORM_LABELS[p] for p in empty)
            raise MetaNoDataError(f"No {names} data is available for the selected dates yet "
                                  f"({'; '.join(reasons.values())}).",
                                  details={"platforms": empty, "reasons": reasons,
                                           "period": context.get("requested_period")})

    # ---- report ----------------------------------------------------------------------------------

    def build(self, request, context, external=None, notes=()):
        now, identifier = datetime.now(timezone.utc), uuid.uuid4().hex
        period = context.get("requested_period") or {}
        period_text = f"{period.get('from_date')} to {period.get('to_date')}"
        tables: list[ReportTable] = []
        sections: list[DisplaySection] = []
        content_sections: list[dict[str, str]] = []
        charts: list[ReportChart] = []
        limitations: list[str] = list(notes)
        key_metrics: list[KeyMetric] = []
        display_metrics: list[DisplayMetric] = []
        highlights: list[str] = []
        facts: list[dict[str, Any]] = []
        data_points = 0

        if external is not None:
            tables.extend(t.model_copy(update={"id": "shipments_" + (t.id or str(i))})
                          for i, t in enumerate(external.tables))
            for section in external.structured_report.sections if external.structured_report else []:
                blocks = [b.model_copy(update={"id": "shipments_" + (b.id or str(len(sections)))})
                          if isinstance(b, TableBlock) else b for b in section.blocks]
                sections.append(section.model_copy(update={"id": "shipments_" + section.id,
                                                           "order": len(sections) + 1, "blocks": blocks}))
            if external.structured_report:
                charts.extend(c.model_copy(update={"id": "shipments_" + c.id}) for c in external.structured_report.charts)
                display_metrics.extend(m.model_copy(update={"key": "shipments_" + m.key})
                                       for m in external.structured_report.metrics)
            key_metrics.extend(external.key_metrics)
            content_sections.extend(s.model_dump() for s in external.content.sections)
            limitations.extend(external.content.data_limitations)

        platforms_used = []
        for platform in request.meta_sources:
            block = context["sources"][platform]
            accounts = block.get("accounts") or []
            platforms_used.append(f"{PLATFORM_LABELS[platform]} ({len(accounts)} account"
                                  f"{'' if len(accounts) == 1 else 's'})")
            if len(accounts) > MAX_ACCOUNTS_PER_PLATFORM:
                limitations.append(f"{PLATFORM_LABELS[platform]}: the first {MAX_ACCOUNTS_PER_PLATFORM} of "
                                   f"{len(accounts)} connected accounts are shown.")
            for index, account in enumerate(accounts[:MAX_ACCOUNTS_PER_PLATFORM], start=1):
                alias = f"{PLATFORM_LABELS[platform]} account {index}"
                name = _text(account.get("account_name") or alias, 80)
                title = f"{PLATFORM_LABELS[platform]}: {name}"
                section_id = f"meta_{platform}_{index}"
                blocks: list[Any] = [ParagraphBlock(type="paragraph", text=_freshness(account, period_text))]
                metrics = account.get("metrics") or {}
                rows, values = [], []
                for metric, entry in metrics.items():
                    for label, value, basis in _metric_rows(metric, entry):
                        rows.append([_text(label, 120), fmt(value), _text(basis, 200)])
                        values.append([label, value, basis])
                        facts.append({"source": alias, "platform": platform, "metric": label, "value": value,
                                      "basis": basis})
                    data_points += (len(entry.get("daily") or []) or len(entry.get("by_dimension") or [])
                                    or (1 if entry.get("kind") == "snapshot" else 0))
                if rows and len(tables) < MAX_TABLES:
                    table = ReportTable(
                        id=f"{section_id}_metrics", title=_text(f"{title}: metrics", 200),
                        columns=["Metric", "Value", "Basis"], rows=rows[:200], values=values[:200],
                        column_meta=[TableColumn(key="metric", label="Metric"),
                                     TableColumn(key="value", label="Value", type="number", align="right"),
                                     TableColumn(key="basis", label="Basis")],
                        total_rows=len(rows), truncated=len(rows) > 200,
                        note="Synced from Meta. Totals are given only for metrics that can be added across days.")
                    tables.append(table)
                    blocks.append(TableBlock(**table.model_dump()))
                elif not rows:
                    blocks.append(ParagraphBlock(type="paragraph", text="No synced data for the selected dates."))
                item_table = _items_table(section_id, title, account.get("items") or {})
                if item_table is not None and len(tables) < MAX_TABLES:
                    tables.append(item_table)
                    blocks.append(TableBlock(**item_table.model_dump()))
                unavailable = account.get("unavailable_metrics") or []
                if unavailable:
                    items = [_text(f"{metric_label(u['metric'])}: {REASONS.get(u.get('reason'), str(u.get('reason')).replace('_', ' '))}", 500)
                             for u in unavailable[:12]]
                    blocks.append(ParagraphBlock(type="paragraph", text="Not included:"))
                    blocks.append(BulletBlock(type="bullet_list", items=items))
                chart = _trend_chart(section_id, title, platform, metrics)
                if chart is not None:
                    charts.append(chart)
                for metric in HEADLINES[platform]:
                    entry = metrics.get(metric)
                    value = None if entry is None else entry.get("total", entry.get("value"))
                    if value is None:
                        continue
                    label = metric_label(metric)
                    basis = "as of " + _day(entry.get("as_of")) if entry.get("kind") == "snapshot" else period_text
                    if len(highlights) < 5:
                        highlights.append(_text(f"{name}: {fmt(value)} {label.lower()} ({basis})", 500))
                    if len(key_metrics) < 30:
                        key_metrics.append(KeyMetric(label=_text(f"{PLATFORM_LABELS[platform]} {label.lower()}", 120),
                                                     value=fmt(value), note=_text(name, 300)))
                    display_metrics.append(DisplayMetric(key=f"{section_id}_{metric}", label=_text(label, 120),
                                                         value=value, unit="number", note=_text(name, 200)))
                sections.append(DisplaySection(id=section_id, title=_text(title, 200), order=len(sections) + 1,
                                               blocks=blocks[:12]))
                content_sections.append({"heading": _text(title, 200), "content": _text(
                    "\n".join([blocks[0].text] + [f"{r[0]}: {r[1]} ({r[2]})" for r in rows[:60]]), 8000)})
            limitations.extend(block.get("caveats") or [])
            if block.get("status") == "no_data":
                limitations.append(f"{PLATFORM_LABELS[platform]}: {_empty_reason(block)}.")

        limitations.append("Values from different sources and accounts are reported separately and never added "
                           "together; WhatsApp, Instagram and Facebook data cannot be linked to individual "
                           "shipments.")
        summary = (f"Report for {period_text} covering " + ", ".join(platforms_used)
                   + (" and shipments." if external is not None else ".")
                   + " All social figures are synced from Meta for the seller's connected accounts.")
        paragraphs = [summary]
        if external is not None and external.structured_report:
            paragraphs.extend(external.structured_report.executive_summary.paragraphs[:2])
        title = ("Shipments and social media report" if external is not None else "Social media report")
        report_type = "combined" if external is not None else "social_media"
        limitations = list(dict.fromkeys(limitations))
        report = ReportResponse(
            report_id=identifier, report_type=report_type, report_name=title, generated_at=now,
            prompt=request.prompt, ai_model=getattr(self.ai, "model", None) or "none",
            records_analyzed=data_points + (external.records_analyzed if external is not None else 0),
            key_metrics=key_metrics[:30], tables=tables[:MAX_TABLES],
            content=ReportContent(title=title, executive_summary=_text(" ".join(paragraphs), 6000),
                                  sections=content_sections[:50], data_limitations=limitations[:15]),
            structured_report=StructuredReport(
                report_id=identifier, report_type=report_type, title=title,
                subtitle=f"{', '.join(PLATFORM_LABELS[p] for p in request.meta_sources)} · {period_text}",
                generated_at=now, executive_summary=StructuredSummary(paragraphs=[_text(p, 1200) for p in paragraphs[:4]],
                                                                      highlights=highlights[:5]),
                metrics=display_metrics, sections=sections[:50], findings=[], recommendations=[],
                limitations=limitations, charts=charts,
                scope={"source": "meta_report_context", "data_sources": request.data_sources,
                       "selected_period": period, "record_type": "synced daily metric values",
                       "comparison_rules": context.get("comparison_rules") or [],
                       **({"shipment_scope": external.structured_report.scope}
                          if external is not None and external.structured_report else {})},
                company_name=request.company_name, customer_id=request.customer_id),
            company_name=request.company_name, customer_id=request.customer_id,
            metrics={"meta_sources": {p: {"status": context["sources"][p].get("status")} for p in request.meta_sources}},
            generation={"ai_calls": (external.generation or {}).get("ai_calls", 0) if external is not None else 0,
                        "narrative_source": "verified_meta_data", "data_sources": request.data_sources})
        return report, facts

    # ---- optional AI observations ------------------------------------------------------------------

    async def _observations(self, prompt: str, facts: list[dict[str, Any]], context) -> tuple[list[ReportFinding], int]:
        """Up to 4 observations grounded in the supplied values; any finding quoting another number is dropped."""
        from app.config import settings
        if not facts or self.ai is None or not settings.deepseek_configured:
            return [], 0
        payload = {"question": prompt or "Summarise the most important changes and results.",
                   "period": context.get("requested_period"), "facts": facts[:150],
                   "rules": (context.get("comparison_rules") or []) + [
                       "Use only the facts given. Quote numbers exactly as given; do not calculate new numbers, "
                       "percentages or sums.",
                       "Refer to sources by the given source names only."]}
        messages = [
            {"role": "system", "content": "You write short, factual observations for a seller's social media "
             "report. Return JSON {\"findings\": [{\"title\": str, \"detail\": str}]} with at most 4 findings."},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=True, default=str)}]
        try:
            raw = await self.ai.generate_json(messages, max_tokens=900)
        except AIPrivacyError:
            return [], 0
        except Exception:   # noqa: BLE001 - observations are optional; the verified tables stand alone
            return [], 1
        allowed = _allowed_numbers(facts, context)
        findings = []
        for item in (raw.get("findings") if isinstance(raw, dict) else None) or []:
            if not isinstance(item, dict):
                continue
            title, detail = str(item.get("title") or "").strip(), str(item.get("detail") or "").strip()
            if not title or not detail or not _grounded(f"{title} {detail}", allowed):
                continue
            findings.append(ReportFinding(title=_text(title, 250), detail=_text(detail, 3000), severity="info"))
        return findings[:4], 1


# ---- helpers ----------------------------------------------------------------------------------------

def _metric_rows(metric: str, entry: dict[str, Any]):
    label = metric_label(metric)
    if entry.get("kind") == "snapshot":
        yield label, entry.get("value"), f"As of {_day(entry.get('as_of'))}"
        return
    days = entry.get("days_with_data")
    if "by_dimension" in entry:
        for row in entry["by_dimension"][:25]:
            additive = "total" in row
            yield (f"{label} - {row.get('dimension')}", row.get("total") if additive else row.get("latest"),
                   f"Total of {row.get('days_with_data')} days with data" if additive else "Latest daily value")
    elif "total" in entry:
        yield label, entry["total"], f"Total of {days} days with data"
    else:
        latest = (entry.get("daily") or [{}])[-1].get("date")
        yield label, entry.get("latest"), f"Daily value on {latest} (cannot be added across days)"


def _items_table(section_id: str, title: str, items: dict[str, Any]) -> ReportTable | None:
    preferred = ("media_views", "post_media_view", "media_total_interactions", "post_clicks", "media_reach")
    name = next((m for m in preferred if m in items), next(iter(items), None))
    if name is None or not items[name].get("items"):
        return None
    top = items[name]["items"][:10]
    label = metric_label(name)
    rows = [[_text(i.get("id"), 60), _text(i.get("type") or "-", 40), _day(i.get("published_at")), fmt(i.get("value"))]
            for i in top]
    values = [[i.get("id"), i.get("type"), _day(i.get("published_at")), i.get("value")] for i in top]
    return ReportTable(id=f"{section_id}_top", title=_text(f"{title}: top posts by {label.lower()}", 200),
                       columns=["Post ID", "Type", "Published", label[:100]], rows=rows, values=values,
                       column_meta=[TableColumn(key="id", label="Post ID"), TableColumn(key="type", label="Type"),
                                    TableColumn(key="published", label="Published", type="date"),
                                    TableColumn(key="value", label=label[:100], type="number", align="right")],
                       total_rows=len(items[name]["items"]), truncated=len(items[name]["items"]) > 10,
                       note="Lifetime values per post as last synced.")


def _trend_chart(section_id: str, title: str, platform: str, metrics: dict[str, Any]) -> ReportChart | None:
    for metric in HEADLINES[platform] + tuple(metrics):
        entry = metrics.get(metric) or {}
        daily = entry.get("daily") or []
        if entry.get("additive") and len(daily) >= 2 and all((d.get("value") or 0) >= 0 for d in daily):
            label = metric_label(metric)
            return ReportChart(id=f"{section_id}_trend", title=_text(f"{title}: daily {label.lower()}", 200),
                               type="line", labels=[d["date"] for d in daily[:200]],
                               datasets=[ChartDataset(label=label, data=[float(d["value"] or 0) for d in daily[:200]])],
                               x_axis=ChartAxis(label="Date", format="date"), y_axis=ChartAxis(label=label,
                                                                                               format="number"),
                               source="rule")
    return None


def _freshness(account: dict[str, Any], period_text: str) -> str:
    coverage = account.get("coverage") or {}
    parts = []
    if coverage.get("days_with_data"):
        parts.append(f"Data covers {coverage.get('first_date')} to {coverage.get('last_date')} "
                     f"({coverage.get('days_with_data')} of {coverage.get('requested_days')} days requested).")
    else:
        parts.append(f"No daily data is synced for {period_text}.")
    synced = account.get("last_successful_sync_at")
    parts.append(f"Last successful sync: {str(synced)[:16].replace('T', ' ')} UTC." if synced
                 else "This account has not completed a sync yet.")
    if account.get("reconnect_required"):
        parts.append("This account must be reconnected before newer data can be synced.")
    elif account.get("data_stale"):
        parts.append("This data may be out of date: the latest sync is older than expected.")
    return _text(" ".join(parts), 1200)


def _empty_reason(block: dict[str, Any]) -> str:
    reasons = {u.get("reason") for a in block.get("accounts") or [] for u in a.get("unavailable_metrics") or []}
    if reasons and reasons <= {"not_synced"}:
        return "the first sync has not finished yet"
    if not any(a.get("last_successful_sync_at") for a in block.get("accounts") or []):
        return "the first sync has not finished yet"
    return "Meta returned no data for these dates"


def _numbers(value: float) -> set[str]:
    out = {f"{value:,.0f}", f"{value:.0f}"} if float(value).is_integer() else set()
    out |= {f"{value:,.2f}", f"{value:.2f}", f"{value:,.1f}", f"{value:.1f}", f"{value:g}"}
    return out


def _allowed_numbers(facts: list[dict[str, Any]], context: dict[str, Any]) -> set[str]:
    allowed: set[str] = set()
    for fact in facts:
        if isinstance(fact.get("value"), (int, float)):
            allowed |= _numbers(float(fact["value"]))
        allowed |= set(_NUMBER.findall(str(fact.get("basis") or "")))
        allowed |= set(_NUMBER.findall(str(fact.get("source") or "")))
    period = context.get("requested_period") or {}
    for key in ("from_date", "to_date"):
        try:
            day = date.fromisoformat(str(period.get(key)))
            allowed |= {str(day.year), str(day.day), f"{day.day:02d}"}
        except ValueError:
            pass
    return allowed


def _grounded(text: str, allowed: set[str]) -> bool:
    return all(n in allowed for n in _NUMBER.findall(_ISO_DATE.sub(" ", text)))
