"""Report orchestration: analysis -> AI narrative -> validated report -> exports."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from logging.handlers import RotatingFileHandler
import json
import re
from dataclasses import replace
import uuid
import secrets
import time
import threading
from collections import Counter, OrderedDict
from contextvars import ContextVar
from typing import Any
from fastapi import HTTPException
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.analysis import (ai_plan_messages, annotate_table, build_plan_tables, dataset_scope, get_vocabulary, interpret,
                          matching_rows, merge_ai_plan, plan_narrative, remove_false_absence_claims, verify_report)
from app.analysis.completeness import SelectedFilters, check_source, selected_filters, with_report_filter
from app.analysis.accuracy import DatasetSnapshot, validate_final, verify_revision, seal, digest, immutable_data
from app.errors import NoMatchingRecordsError, AIPrivacyError
from app.services.shipment_source import ShipmentSource, build_query, delegated_credential
from app.analysis.interpreter import report_title
from app.config import BASE_DIR, Settings, settings
from app.errors import (AIResponseError, AppError, ClarificationRequiredError, ReportExportError, request_id_var, report_id_var,
                        InvalidReportDataError, InvalidPromptError, UnsupportedRequestError)
from app.skills.prompt_skill import prompt_skill
from app.generators.docx_generator import generate_docx
from app.generators.pdf_generator import generate_pdf
from app.models.report_schema import (
    DEFAULT_DISCLAIMER,
    ReportContent, KeyMetric, ReportTable, ReportFinding, ReportRecommendation,
    ReportDocument,
    ReportRequest,
    ReportResponse,
    ReportTypeInfo,
    StructuredNarrative, StructuredReport, StructuredSummary, DisplaySection,
    ParagraphBlock, BulletBlock, TableBlock, DisplayMetric, ReportChart, ChartDataset,
)
from app.charts import ChartRequest, build_charts, chart_planning_instructions, get_adapter, plan_charts
from app.charts.edit import ChartEdit, apply_chart_edit, plan_chart_edit
from app.prompts.base import build_messages
from app.prompts.report_intent import (detect_intent, scope_analysis, selected_tables, display_metrics,
                                      report_charts, deterministic_narrative, shape_narrative, relevant_metrics)
from app.prompts.registry import get_report_definition, list_report_definitions
from app.services.deepseek_service import DeepSeekService
from app.services import audit_db
from app.services.report_activity import write_report_activity
from app.services.shipment_insights import build_insights, headline_cards, insights_narrative
from app.services.report_evidence import (NarrativeRepairError, repair_unsupported_narrative,
                                          safe_rejections, validate_numerical_claims)

logger = logging.getLogger(__name__)

_AUDIT_LOCK = threading.Lock()


def _log_validation_event(event):
    event = {**event, "request_id": request_id_var.get(), "report_id": report_id_var.get()}
    terminal_event = {key: value for key, value in event.items() if key not in {"context", "verified_metrics"}}
    logger.warning("%s", json.dumps(terminal_event, ensure_ascii=True))
    # Persist safe operational diagnostics, not narrative/customer text.
    audit = logging.getLogger("ai_box.validation_audit")
    try:
        with _AUDIT_LOCK:
            if not any(isinstance(h, RotatingFileHandler) for h in audit.handlers):
                directory = BASE_DIR / "output"
                directory.mkdir(parents=True, exist_ok=True)
                handler = RotatingFileHandler(directory / "report_validation.jsonl", maxBytes=1_000_000,
                                              backupCount=2, encoding="utf-8")
                handler.setFormatter(logging.Formatter("%(message)s"))
                audit.addHandler(handler)
                audit.setLevel(logging.WARNING)
            audit.propagate = False
        handler = next(h for h in audit.handlers if isinstance(h, RotatingFileHandler))
        # Keep the claim context (already PII-redacted by safe_rejections, max 200 chars) so a
        # rejection can be diagnosed from this file alone; never the full narrative.
        message = json.dumps(event, ensure_ascii=True)
        handler.handle(logging.LogRecord(audit.name, logging.WARNING, __file__, 0, message, (), None))
    except OSError:
        logger.warning("Validation audit file is unavailable; diagnostics remain in console logs.")
    audit_db.submit("validation", event)


_IST = timezone(timedelta(hours=5, minutes=30))

# Request-local processing stage (date_resolution, planning, source_fetch, analysis, narrative, completed),
# recorded in the report activity audit when generation stops.
report_stage_var: ContextVar[str | None] = ContextVar("report_stage", default=None)


def seller_reference(user_id: str | None) -> str | None:
    """A keyed, non-reversible seller pseudonym for the audit trail (never the seller ID itself)."""
    if not user_id:
        return None
    from app.analysis.accuracy import _SIGNING_KEY
    return "s_" + hmac.new(_SIGNING_KEY, b"seller:" + str(user_id).encode(), hashlib.sha256).hexdigest()[:24]


def _period_basis() -> str:
    from app.report_period import effective_period_var
    period = effective_period_var.get()
    return period.source if period else "selected_filters"


def _date_range_scope() -> dict[str, Any]:
    """scope.date_range: the effective period, its source and the six-month check (see report_period)."""
    from app.report_period import effective_period_var
    period = effective_period_var.get()
    return {"date_range": period.to_dict()} if period else {}


def _filename_part(value: Any, fallback: str) -> str:
    """ASCII letters, digits and hyphens only: safe in Content-Disposition and on every file system."""
    text = re.sub(r"[^A-Za-z0-9]+", "-", str(value or "")).strip("-")[:60].strip("-")
    return text or fallback


def export_filename(seller: Any, report_name: Any, generated_at: datetime, extension: str) -> str:
    """Bigship_<Seller>_<ReportName>_<Date>.<ext>, dated in India time."""
    day = (generated_at if generated_at.tzinfo else generated_at.replace(tzinfo=timezone.utc)).astimezone(_IST)
    return (f"Bigship_{_filename_part(seller, 'Seller')}_{_filename_part(report_name, 'Report')}_"
            f"{day:%Y-%m-%d}.{extension}")


_COVERAGE_LOCK = threading.Lock()


def write_coverage_audit(record: dict[str, Any]) -> None:
    """One JSON line per shipment report attempt in output/report_coverage_audit.jsonl (rotating, never served).

    Holds what the report covered (source counts, selected filters, upstream query, verification) for audit;
    this is not shown in the report. No credential, prompt text or shipment row is written.
    """
    audit = logging.getLogger("ai_box.coverage_audit")
    try:
        with _COVERAGE_LOCK:
            if not any(isinstance(h, RotatingFileHandler) for h in audit.handlers):
                directory = BASE_DIR / "output"
                directory.mkdir(parents=True, exist_ok=True)
                handler = RotatingFileHandler(directory / "report_coverage_audit.jsonl", maxBytes=5_000_000,
                                              backupCount=5, encoding="utf-8")
                handler.setFormatter(logging.Formatter("%(message)s"))
                audit.addHandler(handler)
                audit.setLevel(logging.INFO)
            audit.propagate = False
            audit.info(json.dumps(record, ensure_ascii=True, default=str))
    except OSError:
        logger.warning("Coverage audit file is unavailable; report_id=%s outcome=%s",
                       record.get("report_id"), record.get("outcome"))
    audit_db.submit("coverage", record)


def _coverage_record(request, user_id, outcome, *, report_id=None, completeness=None, notes=(), details=None):
    return {"event": "report_coverage", "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "request_id": request_id_var.get(), "report_id": report_id, "user_id": user_id,
            "report_type": request.report_type, "source": request.source, "outcome": outcome,
            "completeness": completeness, "notes": list(notes), "details": details}


DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MEDIA_TYPE = "application/pdf"
PPTX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


# Private, bounded, process-local store. A token grants access to exactly one report.
_REPORTS = OrderedDict()
_STORE_LOCK = threading.Lock()
_STORE_TTL = 24 * 60 * 60


def _previous_charts(prev: Any) -> list[dict[str, Any]]:
    """The charts of the report a follow-up refers to, as posted by the client."""
    structured = prev.get("structured_report") if isinstance(prev, dict) else None
    charts = structured.get("charts") if isinstance(structured, dict) else None
    return [c for c in charts if isinstance(c, dict)] if isinstance(charts, list) else []


def _clean(value, limit):
    """Stripped text; over-long text is cut at a sentence end so no number is split."""
    text = value.strip() if isinstance(value, str) else ""
    if len(text) <= limit:
        return text
    cut = max(text.rfind(mark, 0, limit) for mark in (". ", "! ", "? "))
    return text[:cut + 1] if cut > 0 else ""


def _texts(value, limit, count):
    items = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    return [t for t in (_clean(item, limit) for item in items) if t][:count]


def _normalize_structure(raw):
    """Local cleanup of a structurally invalid AI report, used instead of a second AI call.

    Keeps only the AI's own text in the supported shape: unknown fields and unsupported
    blocks (inline tables, charts) are dropped, common alternative names are mapped, and
    ids/orders are renumbered. Numbers are checked afterwards like any other AI output.
    """
    summary = raw.get("executive_summary")
    summary = {"paragraphs": summary} if isinstance(summary, (str, list)) else summary if isinstance(summary, dict) else {}
    sections, seen_tables = [], set()
    for section in raw.get("sections") if isinstance(raw.get("sections"), list) else []:
        if not isinstance(section, dict):
            continue
        source = section.get("blocks")
        if not isinstance(source, list):
            source = [{"type": "paragraph", "text": t} for t in str(section.get("content") or "").split("\n\n")]
        blocks = []
        for block in source:
            block = {"type": "paragraph", "text": block} if isinstance(block, str) else block
            if not isinstance(block, dict):
                continue
            kind = str(block.get("type", "")).lower()
            if kind in ("paragraph", "text"):
                text = _clean(block.get("text") or block.get("content"), 1200)
                if text:
                    blocks.append({"type": "paragraph", "text": text})
            elif kind in ("bullet_list", "bullets", "bullet", "list"):
                items = _texts(block.get("items"), 500, 12)
                if items:
                    blocks.append({"type": "bullet_list", "items": items})
            elif kind == "table_reference" and re.fullmatch(r"table_[0-9]+", str(block.get("table_id", ""))):
                if block["table_id"] not in seen_tables:
                    seen_tables.add(block["table_id"])
                    blocks.append({"type": "table_reference", "table_id": block["table_id"]})
        title = _clean(section.get("title") or section.get("heading"), 200)
        if title and blocks:
            sections.append({"id": f"section_{len(sections) + 1}", "title": title,
                             "order": len(sections) + 1, "blocks": blocks[:12]})
    findings = []
    for item in raw.get("findings") if isinstance(raw.get("findings"), list) else []:
        if isinstance(item, dict):
            title, detail = _clean(item.get("title"), 250), _clean(item.get("detail") or item.get("description"), 3000)
            if title and detail:
                findings.append({"title": title, "detail": detail, "severity": item.get("severity")})
    recommendations = []
    for item in raw.get("recommendations") if isinstance(raw.get("recommendations"), list) else []:
        if isinstance(item, dict):
            action = _clean(item.get("action") or item.get("recommendation"), 600)
            if action:
                recommendations.append({"action": action, "rationale": _clean(item.get("rationale"), 3000),
                                        "priority": item.get("priority")})
    return {
        "title": _clean(raw.get("title"), 200) or "Report",
        "executive_summary": {"heading": _clean(summary.get("heading"), 200) or "Executive Summary",
                              "paragraphs": _texts(summary.get("paragraphs"), 1200, 4),
                              "highlights": _texts(summary.get("highlights"), 500, 5)},
        "sections": sections[:50],
        "findings": findings[:30],
        "recommendations": recommendations[:20],
        "data_limitations": _texts(raw.get("data_limitations"), 1000, 30),
    }


_TOPICS = ("financial", "courier", "payment", "cancellation", "geography", "exception", "status")


def _plan_intent(intent, plan, vocab):
    """Let the validated plan decide scope and which sections appear; formatting choices stay with the intent."""
    status = plan.single_status(vocab.status_field)
    topics = list(intent.topics)
    topics += [t for t in plan.topics if t in _TOPICS and t not in topics]
    if plan.lookup and status:
        topics = [t for t in topics if t != "status"] or ["courier"]  # a one-status summary table says nothing
    if plan.detail_level != "detailed" and any(t.comparison for t in plan.tables):
        topics = []  # the comparison table answers the request; no unrelated overview tables
    changes = {"status": status, "topics": tuple(topics)}
    if plan.detail_level != "detailed" or plan.lookup:
        # Unrequested charts are not added to focused requests; a record list needs no recommendations.
        changes.update(focused=True, charts=intent.charts and plan.wants_charts,
                       recommendations=intent.recommendations and not plan.lookup)
    return replace(intent, **changes)


_COMPARE = re.compile(r"\b(?:compar\w*|vs\.?|versus|against)\b")


def _comparison_chart(plan, chart_plan, adapter, prompt):
    """The chart for a requested comparison: same grouping and measure as its table, computed by the chart engine."""
    spec = next((t for t in plan.tables if t.kind == "grouped" and t.comparison), None)
    if spec is None or not (plan.wants_charts or _COMPARE.search((prompt or "").lower())):
        return None
    rate = next((m for m in spec.columns if adapter.measures[m].agg == "rate"), None)
    value = rate or next((m for m in spec.columns if m != adapter.default_measure), adapter.default_measure)
    requested = {r.type for r in chart_plan.requests}
    if spec.series:
        kind = next((k for k in ("heatmap", "stacked_bar", "grouped_bar") if k in requested), "grouped_bar")
        if kind == "stacked_bar" and not adapter.measures[value].additive:
            kind = "grouped_bar"  # rates cannot be stacked
    else:
        kind = next((k for k in ("horizontal_bar", "line", "bar") if k in requested), "bar")
    return ChartRequest(type=kind, dimension=spec.group_by, series=spec.series, measure=value,
                        clause=(prompt or "")[:160], source="rule")


def _source_guidance(selected: SelectedFilters, completeness: dict[str, Any]) -> str:
    """The exact selected filters and record counts, so the narrative describes this scope and nothing wider."""
    parts = []
    if selected.from_date or selected.to_date:
        parts.append(f"{selected.date_field.replace('_', ' ')} {selected.from_date or '...'} to {selected.to_date or '...'} "
                     f"({selected.timezone})")
    parts.append(f"status {selected.status_label or 'All'}")
    text = (f"Selected filters: {'; '.join(parts)}. The source matched {completeness['expected_records']} records, "
            f"{completeness['unique_records']} unique were received and {completeness['analyzed_records']} are analysed. "
            if completeness["expected_records"] is not None else f"Selected filters: {'; '.join(parts)}. ")
    if selected.status:
        text += (f"Every record has the selected status ({selected.status_label}); this cohort cannot establish "
                 "account-wide delivery, RTO or cancellation rates, so never state such rates. ")
    return text


def _scope_conflict(plan, selected: SelectedFilters, as_of) -> None:
    """A prompt asking for a period or status outside the selection is clarified, never silently re-scoped."""
    period = plan.date_range
    if period and period.field == selected.date_field and (selected.from_date or selected.to_date):
        end = min(period.end, as_of) if as_of else period.end  # "this month" is not a request for future days
        if (selected.from_date and period.start < selected.from_date) or (selected.to_date and end > selected.to_date):
            sel = f"{selected.from_date or '...'} to {selected.to_date or '...'}"
            raise ClarificationRequiredError(
                f"The prompt asks for {period.label} ({period.describe()}), but the selected period is {sel}. "
                "Change the selected dates to cover that period and reload, or remove the period from the prompt.",
                details={"conflict": "period", "selected": selected.to_dict(),
                         "requested": {"from": period.start.isoformat(), "to": period.end.isoformat(),
                                       "requested": period.label},
                         "options": ["change_selected_period", "use_selected_period"]})
    wanted = plan.single_status(get_vocabulary("shipment").status_field)
    if wanted and selected.status and wanted != selected.status:
        raise ClarificationRequiredError(
            f"The prompt asks about {wanted.replace('_', ' ')} shipments, but the selected status is "
            f"{selected.status_label}. Change the selected status and reload, or remove the status from the prompt.",
            details={"conflict": "status", "selected": selected.to_dict(), "requested": {"status": wanted},
                     "options": ["change_selected_status", "use_selected_status"]})


def _plan_guidance(plan, scope, vocab, adapter, source_text=""):
    """Plan facts for the single DeepSeek call: what the data covers, how long to be, what is unavailable."""
    text = source_text
    parts = plan.describe_scope(adapter, vocab)
    if parts:
        text += ("The dataset is already filtered to: " + "; ".join(parts) + ". Describe results as this subset only; "
                 "never generalise them to all shipments. ")
    if scope and scope.get("is_partial"):
        text += "The supplied records are a partial extract of the account; never describe them as all orders. "
    if plan.detail_level == "standard":
        text += "Answer only what was asked: at most three short sections and three findings, no generic advice. "
    if any(t.comparison for t in plan.tables):
        text += ("The requested comparison has been calculated in metrics.comparisons (rates use each row's own orders); "
                 "describe it from those rows and never call it unavailable. ")
    if plan.unsupported:
        text += ("These requested items are not in the data; do not estimate them, mention them once in data_limitations: "
                 + "; ".join(u.requested for u in plan.unsupported) + ". ")
    return text


class ReportService:
    def __init__(self, ai_service: DeepSeekService, config: Settings = settings,
                 source: ShipmentSource | None = None) -> None:
        self._ai = ai_service
        self._settings = config
        self._prompt_skill = prompt_skill
        self._source = source or ShipmentSource(config)

    def list_report_types(self) -> list[ReportTypeInfo]:
        return [
            ReportTypeInfo(
                report_type=d.report_type,
                display_name=d.display_name,
                description=d.description,
                default_prompt=d.default_prompt,
                accepted_data_paths=list(d.accepted_data_paths),
            )
            for d in list_report_definitions()
        ]

    def _summarize_data_for_suggestions(
        self,
        report_type: str,
        data: dict[str, Any],
        definition: Any,
    ) -> dict[str, Any] | None:
        if not isinstance(data, dict):
            return None

        summary: dict[str, Any] = {}
        try:
            analysis = definition.analyze(data)
            metrics = analysis.metrics or {}
            summary["records_analyzed"] = analysis.records_analyzed

            if "status_category_breakdown" in metrics and metrics["status_category_breakdown"]:
                summary["status_counts"] = {k: v for k, v in metrics["status_category_breakdown"].items() if v}
            elif "status_distribution" in metrics:
                summary["status_counts"] = metrics["status_distribution"]
            elif "status_breakdown" in metrics:
                summary["status_counts"] = dict(list(metrics["status_breakdown"].items())[:6])

            if "courier_performance" in metrics:
                couriers = [
                    c.get("courier") for c in metrics["courier_performance"]
                    if isinstance(c, dict) and c.get("courier")
                ]
                if couriers:
                    summary["available_couriers"] = couriers[:8]

            if "top_destination_cities" in metrics:
                cities = [c.get("city") for c in metrics["top_destination_cities"] if isinstance(c, dict) and c.get("city")]
                if cities:
                    summary["top_cities"] = cities[:6]

            if "payment_mode_breakdown" in metrics and metrics["payment_mode_breakdown"]:
                summary["payment_modes"] = metrics["payment_mode_breakdown"]

            if "cancellation_reasons" in metrics:
                reasons = [r.get("reason") for r in metrics["cancellation_reasons"] if isinstance(r, dict) and r.get("reason")]
                if reasons:
                    summary["cancellation_reasons"] = reasons[:5]

            if "total_remitted_amount" in metrics:
                summary["total_remitted_amount"] = metrics["total_remitted_amount"]
            if "total_pending_amount" in metrics:
                summary["total_pending_amount"] = metrics["total_pending_amount"]
        except Exception:
            pass

        # Also incorporate high-level account metadata if available
        if "account_summary" in data and isinstance(data["account_summary"], dict):
            summary["account_totals"] = {k: v for k, v in data["account_summary"].items() if v}
        if "report_filter" in data and isinstance(data["report_filter"], dict):
            summary["active_filters"] = data["report_filter"]

        # Raw fallback if analyze didn't produce anything
        if not summary or summary.get("records_analyzed", 0) == 0:
            orders = data.get("getOrderList") or data.get("orders") or data.get("paidRemittanceList") or data.get("pendingRemittanceList")
            if isinstance(orders, dict):
                orders = orders.get("data")
            if isinstance(orders, list) and orders:
                couriers = set()
                statuses = Counter()
                for o in orders[:50]:
                    if isinstance(o, dict):
                        courier = o.get("courier_name") or o.get("courier") or o.get("courierName")
                        if courier:
                            couriers.add(str(courier))
                        status = o.get("status") or o.get("order_status") or o.get("orderStatus")
                        if status:
                            statuses[str(status)] += 1
                summary["records_count"] = len(orders)
                if couriers:
                    summary["available_couriers"] = list(couriers)[:6]
                if statuses:
                    summary["status_counts"] = dict(statuses.most_common(5))

        return summary if summary else None

    async def _generate_data_tailored_suggestions(
        self,
        prompt: str,
        report_type: str,
        data: dict[str, Any],
        definition: Any,
        validation_reason: str,
    ) -> list[str] | None:
        """Use DeepSeek to generate smart, data-grounded prompt suggestions when the user's prompt is invalid."""
        if not self._ai or not self._settings.deepseek_configured:
            return None

        summary = self._summarize_data_for_suggestions(report_type, data, definition)
        if not summary:
            return None

        system_prompt = (
            "You are an expert e-commerce and logistics analytics consultant. "
            "A user attempted to generate a report but did not enter an appropriate or specific prompt. "
            "Your task is to examine the provided dataset summary and generate 3 to 4 concise, diverse, "
            "and highly practical analytical prompts in English that the user can ask to explore their data.\n"
            "Guidelines:\n"
            "1. Prompts must be analytical questions or instructions (e.g. comparing courier delivery performance, "
            "breakdown of delivery statuses, RTO patterns, top destination cities, payment mode analysis, or charts).\n"
            "2. Ground each prompt in the actual data: refer specifically to the couriers, status names, or numbers "
            "present in the dataset summary when helpful.\n"
            "3. Keep each suggested prompt to 1 or 2 concise, natural sentences.\n"
            "4. Return strict JSON format with a single key 'suggested_prompts' containing a list of 3-4 strings.\n"
            "Example format:\n"
            '{"suggested_prompts": ["Analyse courier delivery performance and RTO rates across Delhivery and Ekart.", "Include a donut chart of delivery status breakdown and top destination cities.", "Compare COD vs prepaid delivery success rates."]}'
        )

        user_content = (
            f"Report Type: {report_type}\n"
            f"User's Entered Prompt: \"{prompt}\"\n"
            f"Validation Issue: {validation_reason}\n"
            f"Dataset Summary:\n{json.dumps(summary, indent=2)}\n\n"
            "Generate 3 to 4 data-tailored analytical prompt suggestions in JSON format."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        try:
            response = await asyncio.wait_for(
                self._ai.generate_json(messages, max_tokens=350),
                timeout=8.0,
            )
            raw_suggestions = response.get("suggested_prompts")
            if isinstance(raw_suggestions, list):
                cleaned = [str(s).strip() for s in raw_suggestions if str(s).strip()]
                if len(cleaned) >= 2:
                    logger.info("DeepSeek generated %d data-tailored prompt suggestions.", len(cleaned))
                    return cleaned[:4]
        except AIPrivacyError:
            raise
        except Exception:
            logger.warning("Could not generate AI prompt suggestions via DeepSeek (details withheld).")

        return None

    async def generate_report(self, request: ReportRequest, *, shipment_credential: str | None = None,
                              user_id: str | None = None) -> ReportResponse:
        report_token = report_id_var.set(uuid.uuid4().hex)
        request_token = request_id_var.set(request_id_var.get() or uuid.uuid4().hex)
        started_at = datetime.now(timezone.utc)
        started = time.monotonic()
        report = None
        outcome, error_code = "cancelled", None
        error_details = {}
        from app.security.privacy import privacy_context
        from app.report_period import effective_period_var, resolve_report_period
        privacy = privacy_context(request.model_dump(mode="json"), {"seller_id": user_id,
                                  "access_token": shipment_credential})
        stage_token = report_stage_var.set("date_resolution")
        period_token = effective_period_var.set(None)
        period, date_check = None, "not_applicable"
        try:
            with privacy:
                # The period is fixed (and limited to six months) before any AI call or shipment query.
                request, period = resolve_report_period(request, default_date_field=self._settings.shipment_api_date_fields[0])
                effective_period_var.set(period)
                date_check = "within_limit" if period else "not_applicable"
                report = await self._generate_report(request, shipment_credential=shipment_credential, user_id=user_id)
            outcome = "generated"
            report_stage_var.set("completed")
            return report
        except Exception as exc:
            outcome = "failed"
            error_code = exc.code if isinstance(exc, AppError) else (
                f"http_{exc.status_code}" if isinstance(exc, HTTPException) else "internal_error")
            if isinstance(exc, NoMatchingRecordsError) and isinstance(exc.details, dict):
                # Counts and fixed reason codes only; never raw records or prompts.
                error_details = {key: exc.details[key] for key in ("reason", "source_records", "pincode_records")
                                 if key in exc.details}
            if report_stage_var.get() == "date_resolution":
                reason = exc.details.get("reason") if isinstance(getattr(exc, "details", None), dict) else None
                date_check = {"report_date_range_exceeded": "exceeded", "invalid_date_range": "invalid"}.get(
                    error_code, {"date_range_conflict": "conflict", "multiple_periods": "ambiguous"}.get(reason, date_check))
            # Fixed internal codes (e.g. the privacy check's residual_person) are audited, never returned.
            internal_code = getattr(exc, "internal_code", None)
            if internal_code:
                error_details["internal_error_code"] = internal_code
            logger.warning("Report generation failed: request_id=%s stage=%s code=%s internal_code=%s",
                           request_id_var.get(), report_stage_var.get(), error_code, internal_code or "-")
            raise
        finally:
            try:
                completed_at = datetime.now(timezone.utc)
                write_report_activity({
                    "event": "report_generation", "request_id": request_id_var.get(),
                    "report_id": report.report_id if report else report_id_var.get(),
                    "seller_id": user_id, "user_id": user_id,
                    "customer_id": report.customer_id if report else request.customer_id,
                    "company_name": request.company_name, "prompt": request.prompt,
                    "report_type": request.report_type, "source": request.source,
                    "filters": request.filters.model_dump(mode="json") if request.filters else None,
                    "date_range": request.date_range,
                    "generation": report.generation if report else None,
                    "started_at": started_at.isoformat(), "completed_at": completed_at.isoformat(),
                    "started_at_ist": started_at.astimezone(_IST).isoformat(),
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "outcome": outcome, "error_code": error_code,
                    **error_details,
                    "allow_partial": request.allow_partial,
                    "is_follow_up": request.previous_report is not None,
                    "previous_report_id": (request.previous_report or {}).get("report_id"),
                    "records_analyzed": report.records_analyzed if report else None,
                    "ai_calls": (report.generation or {}).get("ai_calls") if report else None,
                    "seller_ref": seller_reference(user_id),
                    "processing_stage": report_stage_var.get(),
                    "date_range_source": period.source if period else None,
                    "resolved_start_date": period.start.isoformat() if period else None,
                    "resolved_end_date": period.end.isoformat() if period else None,
                    "date_validation_result": date_check,
                })
            finally:
                effective_period_var.reset(period_token)
                report_stage_var.reset(stage_token)
                report_id_var.reset(report_token)
                request_id_var.reset(request_token)

    async def _generate_report(self, request: ReportRequest, *, shipment_credential: str | None = None,
                               user_id: str | None = None) -> ReportResponse:
        """``shipment_credential``: the caller's delegated shipment-service bearer token (shipment_api mode only).
        ``user_id``: the AI Box user, recorded in the coverage audit log only."""
        definition = get_report_definition(request.report_type)

        # A follow-up that removes or re-types specific charts patches the current report; it is
        # resolved before prompt validation (an edit is not an analytical request) and never
        # regenerated. An unclear target raises ChartEditError and the user's report stays as it is.
        chart_edit = None
        if request.previous_report:
            prev_charts = _previous_charts(request.previous_report)
            chart_edit = plan_chart_edit(request.prompt or "", prev_charts) if prev_charts else None
            if chart_edit and chart_edit.pure:
                if request.filters is not None:
                    previous_scope = (request.previous_report.get("structured_report") or {}).get("scope") or {}
                    if previous_scope.get("selected_period") != selected_filters(request.filters, request.data or {}).to_dict():
                        raise ClarificationRequiredError("The selected report filters changed. Generate a report for the "
                                                         "new period before editing its charts.")
                patched = self._patch_charts(request.previous_report, chart_edit)
                if patched is not None:
                    return patched

        approved_plan = None
        if definition.report_type == "shipment" and self._settings.report_ai_planner:
            from app.analysis.interpreter import plan_request_before_fetch
            # Authenticate delegated access before paying for interpretation; never forward it to AI.
            if request.source == "shipment_api":
                delegated_credential(shipment_credential)
                build_query(request.filters, self._settings)
            report_stage_var.set("planning")
            approved_plan = await plan_request_before_fetch(self._ai, request,
                digest(f"{user_id or 'payload'}:{digest(shipment_credential or '')}")[:20])

        # Validate that user prompt represents a meaningful analytical request
        if approved_plan is None and request.prompt and request.prompt.strip():
            validation = self._prompt_skill.validate(request.prompt, report_type=request.report_type)
            if not validation.is_valid:
                ai_suggestions = await self._generate_data_tailored_suggestions(
                    prompt=request.prompt,
                    report_type=request.report_type,
                    data=request.data,
                    definition=definition,
                    validation_reason=validation.reason,
                )
                if ai_suggestions:
                    validation.suggested_prompts = ai_suggestions

                logger.info(
                    "PromptSkill rejected invalid prompt: category=%s reason=%s prompt=%r suggestion_count=%s",
                    validation.category,
                    validation.reason,
                    "[withheld]",
                    len(validation.suggested_prompts or []),
                )
                raise InvalidPromptError(validation.message, details=validation.to_dict())

        # Source gate: the records must be the complete, de-duplicated set for the selected filters
        # (expected = fetched = unique, all in scope) before any metric, chart or AI call.
        source = selected = None
        authorized_source_scope = None
        upstream_analysis_filters = []
        try:
            if request.source == "shipment_api":
                if approved_plan is not None:
                    request, approved_plan, upstream_analysis_filters, authorized_source_scope = self._push_source_dates(request, approved_plan)
                report_stage_var.set("source_fetch")
                request, source, selected = await self._fetch_source(request, shipment_credential)
            elif definition.report_type == "shipment":
                selected = selected_filters(request.filters, request.data)
                data_in = with_report_filter(request.data, selected) if request.filters else request.data
                source = await run_in_threadpool(check_source, data_in, selected,
                                                 expected_records=request.expected_records,
                                                 allow_partial=request.allow_partial,
                                                 enforce=self._settings.require_complete_source)
                request = request.model_copy(update={"data": source.data})
        except AppError as exc:  # a stopped fetch or gate is audited too (details hold counts, never credentials)
            write_coverage_audit(_coverage_record(request, user_id, exc.code, details=exc.details))
            from app.analysis.accuracy import Audit
            failed_snapshot = DatasetSnapshot("{}", "[]", "{}", "{}", request_id_var.get() or uuid.uuid4().hex,
                                              digest(user_id or "payload")[:20], digest(request.prompt), "unavailable")
            audit = Audit(failed_snapshot, report_id_var.get())
            details = exc.details if isinstance(exc.details, dict) else {}
            audit.add("source_gate", False, details.get("expected_records"), details.get("fetched_records"),
                      "source", "Existing source completeness/scope policy blocked report generation")
            audit.finish(block=False)
            raise

        report_stage_var.set("analysis")
        snapshot = None
        if definition.report_type == "shipment":
            snapshot = DatasetSnapshot.capture(request.data, source, selected,
                user_id=f"{user_id or 'payload'}:{digest(shipment_credential or '')}", prompt=request.prompt)
            # A private working copy from the immutable snapshot is used by every calculation.
            request = request.model_copy(update={"data": immutable_data(json.loads(snapshot.data_json))})

        if approved_plan is not None:
            approved_plan = self._pincode_scope(approved_plan, request)
            adapter = get_adapter(definition.report_type)
            vocab = get_vocabulary(definition.report_type)
            rows = await run_in_threadpool(adapter.rows, request.data)
            if any(f.field == "destination_pincode" for f in approved_plan.filters) and not any(
                    row.get("destination_pincode") is not None for row in rows):
                raise NoMatchingRecordsError(
                    f"{len(rows):,} shipments were received, but none contains a valid destination pincode. "
                    "Pincode analysis cannot be generated from these records.",
                    details={"reason": "required_fields_missing", "source_records": len(rows), "pincode_records": 0,
                             "missing_fields": ["destination_pincode"], "matching_records": 0,
                             "selected_filters": selected.to_dict() if selected else None})
            absent_filters = [f.field for f in approved_plan.filters if not any(row.get(f.field) is not None for row in rows)]
            if absent_filters:
                # Missing data is an unavailable analysis result, not a prompt-editing task.
                # Keep the predicate in every requested section so it is never silently ignored.
                from app.analysis.plan import ExecutionFilter
                unavailable = tuple(ExecutionFilter(field=f.field, op=f.op,
                    value=list(f.value) if isinstance(f.value, tuple) else f.value)
                    for f in approved_plan.filters if f.field in absent_filters)
                approved_plan = replace(approved_plan,
                    filters=tuple(f for f in approved_plan.filters if f.field not in absent_filters),
                    sections=tuple(s.model_copy(update={"filters": (*unavailable, *s.filters),
                        "missing_data": "explicit_unavailable"}) for s in approved_plan.sections))
            scoped, missing = matching_rows(rows, approved_plan)
            context = vocab.context(request.data)
            scope = dataset_scope(context, approved_plan, adapter, vocab, rows, scoped, missing)
            if upstream_analysis_filters:
                scope["authorized_source_scope"] = authorized_source_scope
                scope["source_analysis_filters"] = upstream_analysis_filters
            if not scoped:
                raise NoMatchingRecordsError("No data available for the selected filters and analysis.",
                    details={"reason": "analysis_filters_no_match", "source_records": len(rows),
                             "pincode_records": sum(r.get("destination_pincode") is not None for r in rows),
                             "matching_records": 0, "selected_filters": selected.to_dict() if selected else None})
            return await self._generate_section_report(request, approved_plan, scope, scoped, snapshot, source,
                                                       selected, adapter, approved_plan.planner_calls)

        intent = detect_intent(request.prompt or definition.default_prompt) if request.report_type == "shipment" else None
        chart_adapter = get_adapter(definition.report_type)
        chart_plan = plan_charts(request.prompt or "", definition.report_type, chart_adapter)

        # Prompt -> validated analysis plan -> the records it covers (this request's data only).
        vocab = get_vocabulary(definition.report_type) if intent and chart_adapter else None
        plan = scope = scoped_rows = keep = None
        data = request.data
        planner_calls = 0
        if vocab:
            plan, scope, scoped_rows, keep, planner_calls = await self._build_plan(request, vocab, chart_adapter, intent,
                chart_plan, account_scope=snapshot.account_scope if snapshot else "payload")
            if selected is not None:
                _scope_conflict(plan, selected, await run_in_threadpool(lambda: vocab.context(request.data).as_of))
            if plan.sections:
                if not scoped_rows:
                    raise NoMatchingRecordsError("No data available for the selected filters and analysis.",
                        details={"matching_records": 0, "selected_filters": selected.to_dict() if selected else None})
                return await self._generate_section_report(request, plan, scope, scoped_rows, snapshot, source,
                                                           selected, chart_adapter, planner_calls)
            intent = _plan_intent(intent, plan, vocab)
            if keep is not None and vocab.subset:
                data = vocab.subset(request.data, keep)  # charts and insights see exactly the analysed records
            if plan.date_range:  # the plan's period already scopes the rows; no second, data-anchored window
                chart_plan.requests = [replace(r, window_days=None) for r in chart_plan.requests]

        # The approved plan is resolved before any shipment report metrics are calculated.
        analysis = await run_in_threadpool(definition.analyze, request.data)
        insights = None
        if request.report_type == "shipment" and (keep is None or keep):
            try:
                insights = await run_in_threadpool(build_insights, data)
            except Exception:  # never let the v3 block break the existing report
                logger.exception("Shipment insights could not be built; continuing with the v2 report.")
        if intent:
            analysis = await run_in_threadpool(scope_analysis, analysis, request.data, intent, keep)
        if analysis.records_analyzed == 0:
            logger.info("No data available: request_id=%s report_id=%s; generation stopped before AI/report creation",
                        request_id_var.get(), report_id_var.get())
            raise NoMatchingRecordsError("No data available for the selected filters and analysis.",
                details={"selected_filters": selected.to_dict() if selected else None, "matching_records": 0})
        if chart_plan.chart_only and intent:
            # Charts only: Python computes them and no narrative is needed, so DeepSeek is not called.
            intent = replace(intent, mode="graphs_only", tables=False, charts=True)
        # Plan tables (record lists, groupings, cross-tabs) are calculated before the AI call, so the
        # narrative can describe the comparison that was asked for instead of calling it unavailable.
        plan_tables, plan_notes = [], []
        if plan is not None:
            detail = next((t for t in plan.tables if t.kind == "detail"), None)
            requested = set(detail.columns) - set(vocab.default_detail_columns) if detail else set()
            plan_tables, plan_notes, missing = build_plan_tables(plan, scoped_rows, chart_adapter, vocab, requested)
            plan = replace(plan, unsupported=plan.unsupported + tuple(missing))
            if intent.mode == "graphs_only":
                plan_tables = []
            elif plan_tables and intent.mode == "table_only":
                intent = replace(intent, tables=False)  # the table the user described replaces the standard one
            comparisons = [{"title": t.title, "rows": [dict(zip([c.key for c in t.column_meta], v)) for v in t.values[:40]]}
                           for t in plan_tables if (t.id or "").startswith("plan_grouped")]
            if comparisons:
                analysis.metrics["comparisons"] = comparisons  # AI context and numeric evidence
            comparison_chart = _comparison_chart(plan, chart_plan, chart_adapter, request.prompt) if comparisons else None
            if comparison_chart is not None:
                chart_plan.requests = [r for r in chart_plan.requests if r.resolved and r.source == "rule"
                                       and (r.dimension, r.series) != (comparison_chart.dimension, comparison_chart.series)]
                chart_plan.requests.append(comparison_chart)
        source_text = _source_guidance(selected, source.completeness) if source else ""
        guidance = _plan_guidance(plan, scope, vocab, chart_adapter, source_text) if plan else source_text
        messages = build_messages(definition, analysis, request.prompt, intent, guidance)
        planning = chart_planning_instructions(chart_plan, chart_adapter) if chart_adapter else None
        if planning:  # ambiguous chart requests are planned inside the same single call
            messages[0]["content"] += "\n" + planning[0]
            messages[-1]["content"] += "\n" + planning[1]
        elif chart_plan.requests:
            messages[0]["content"] += "\nRequested charts are calculated by Python and shown separately; do not describe chart values. "
        if sum(len(m["content"]) for m in messages) > 250_000:
            raise InvalidReportDataError("The analysis context is too large. Supply a narrower dataset.")
        narrative_source = "ai"
        # One call only, so it gets the full budget: a cut-off answer is billed and unusable.
        # max_tokens is a cap, not a charge; compact prompts still ask for a short answer.
        output_limit = self._settings.deepseek_max_output_tokens
        outcome = {}
        notice = None
        # Python alone answers record lookups and empty scopes: no DeepSeek call.
        python_only = plan is not None and (not plan.narrative or not analysis.records_analyzed)
        if intent and (intent.deterministic or intent.status and not analysis.records_analyzed or python_only):
            if plan is not None and (plan.scoped or (plan.lookup and not intent.deterministic)):
                title = report_title(list(plan.filters), plan.date_range, chart_adapter, vocab)
                raw_content = plan_narrative(analysis, plan, scope, chart_adapter, vocab, title)
            else:
                raw_content = deterministic_narrative(analysis, intent)
            if chart_plan.chart_only and not (plan is not None and plan.scoped):
                raw_content = {**raw_content, "title": "Shipment Charts",
                               "executive_summary": {"heading": "Overview", "paragraphs": [
                                   "Charts calculated directly from the supplied shipment records."]}}
            narrative_source = "deterministic"
        else:
            try:
                raw_content = await self._generate_verified_content(messages, analysis, request.prompt, max_tokens=output_limit,
                    evidence_metrics=relevant_metrics(analysis, intent) if intent and intent.status else None, outcome=outcome)
            except AIResponseError as exc:
                # The one paid AI answer could not be verified: use the code-written narrative instead
                # of failing the report. Timeouts and service errors are different exception types and still surface.
                if exc.code not in ("ai_unverified_numbers", "ai_invalid_structure") or not self._settings.deterministic_fallback:
                    raise
                if insights is not None and not (intent and intent.status):  # a prompt-narrowed cohort differs from the insights rows
                    raw_content = insights_narrative(insights)
                elif intent:
                    raw_content = deterministic_narrative(analysis, intent)
                else:
                    raise
                logger.warning("AI narrative failed validation (%s); using deterministic narrative. trace_id=%s", exc.code,
                               (exc.details or {}).get("trace_id"))
                narrative_source = "deterministic_fallback"
                notice = ("The AI summary could not be verified against your data, so a standard summary is shown. "
                          "Tables, charts and metrics are calculated directly from your data.")
            else:
                if outcome.get("repaired"):
                    narrative_source = "ai_repaired"
                    notice = ("Some AI statements were removed because their numbers did not match your data. "
                              "All numbers shown are verified.")
        claims_removed = 0
        chosen_tables = selected_tables(analysis, intent) if intent else {f"table_{i}": t for i, t in enumerate(analysis.tables)}
        chosen_tables = {key: annotate_table(table) for key, table in chosen_tables.items()}
        if intent:
            raw_content = shape_narrative(raw_content, analysis, intent)
        if plan is not None and narrative_source != "deterministic":
            # e.g. "No cancellation reasons are available" when the records contain reasons.
            raw_content, claims_removed = remove_false_absence_claims(raw_content, scoped_rows, vocab, vocab.absence_subjects)
            summary = raw_content.get("executive_summary")
            if isinstance(summary, dict) and not summary.get("paragraphs"):
                fallback = plan_narrative(analysis, plan, scope, chart_adapter, vocab, raw_content.get("title") or "Report")
                summary["paragraphs"] = fallback["executive_summary"]["paragraphs"]
            if claims_removed and notice is None:
                notice = ("Statements claiming that available data was missing were removed. "
                          "All numbers shown are verified.")

        try:
            narrative = None
            if isinstance(raw_content.get("executive_summary"), dict):
                narrative = StructuredNarrative.model_validate(raw_content)
                used = set()
                display_sections = []
                legacy_sections = []
                for section in narrative.sections:
                    blocks = []
                    text = []
                    for block in section.blocks:
                        if block.type == "table_reference":
                            index = int(block.table_id.split("_")[1])
                            if index >= len(analysis.tables) or index in used:
                                raise ValueError("Unknown or repeated table reference")
                            used.add(index)
                            blocks.append(TableBlock(**chosen_tables[block.table_id].model_dump()))
                        else:
                            blocks.append(block)
                            text.append(block.text if block.type == "paragraph" else "\n".join("- " + item for item in block.items))
                    display_sections.append(DisplaySection(id=section.id, title=section.title, order=section.order, blocks=blocks))
                    if text:
                        legacy_sections.append({"heading": section.title, "content": "\n\n".join(text)})
                content = ReportContent(title=narrative.title,
                    executive_summary="\n\n".join(narrative.executive_summary.paragraphs),
                    sections=legacy_sections, findings=narrative.findings,
                    recommendations=narrative.recommendations, data_limitations=narrative.data_limitations)
            else:
                content = ReportContent.model_validate(raw_content)
                display_sections = [DisplaySection(id=f"section_{i}", title=section.heading, order=i+1,
                    blocks=[ParagraphBlock(type="paragraph", text=t) for t in section.content.split("\n\n") if t])
                    for i, section in enumerate(content.sections)]
                display_sections.extend(DisplaySection(id=f"table_{i}", title=t.title, order=len(display_sections)+i+1,
                    blocks=[TableBlock(**t.model_dump())]) for i, t in enumerate(analysis.tables))
        except (ValidationError, ValueError) as exc:
            logger.warning("AI report failed schema validation with %d error(s).", len(exc.errors()) if isinstance(exc, ValidationError) else 1)
            raise AIResponseError(
                "The AI response could not be turned into a report, so no report was created. "
                "Please try again or simplify your request.",
                code="ai_invalid_structure",
                details=[
                    {"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]}
                    for err in (exc.errors()[:20] if isinstance(exc, ValidationError) else [])
                ],
            ) from exc
        if plan_tables:
            plan_sections = [DisplaySection(id=f"plan_table_{i + 1}", title=table.title, order=1,
                                            blocks=[TableBlock(**table.model_dump())]) for i, table in enumerate(plan_tables)]
            # A record lookup leads with the records; otherwise the requested tables follow the analysis.
            display_sections = plan_sections + display_sections if plan.lookup else display_sections + plan_sections
            for i, section in enumerate(display_sections):
                section.order = i + 1

        if narrative_source in ("ai", "ai_repaired"):  # code-written narratives quote Python-calculated values only
            try:
                validate_numerical_claims(
                    narrative.model_dump() if narrative else content.model_dump(),
                    analysis.metrics,
                    [table.model_dump() for table in analysis.tables],
                    user_prompt=request.prompt,
                )
            except AIResponseError as exc:
                exc.message = ("The AI report contained numbers that do not match your data, so it was not used and "
                               "no report was created. Please try again, or narrow your request.")
        company = (
            request.company_name
            or (request.data.get("company_name") if isinstance(request.data, dict) else None)
            or (request.data.get("account_summary", {}).get("company_name") if isinstance(request.data, dict) and isinstance(request.data.get("account_summary"), dict) else None)
        )
        customer_id = (
            request.customer_id
            or (request.data.get("customer_id") if isinstance(request.data, dict) else None)
            or (request.data.get("account_summary", {}).get("customer_id") if isinstance(request.data, dict) and isinstance(request.data.get("account_summary"), dict) else None)
        )
        if request.previous_report:
            prev = request.previous_report
            company = company or prev.get("company_name") or (prev.get("structured_report", {}).get("company_name") if isinstance(prev.get("structured_report"), dict) else None)
            customer_id = customer_id or prev.get("customer_id") or (prev.get("structured_report", {}).get("customer_id") if isinstance(prev.get("structured_report"), dict) else None)

        report = ReportResponse(
            report_id=report_id_var.get() or uuid.uuid4().hex,
            report_type=definition.report_type,
            report_name=definition.display_name,
            generated_at=datetime.now(timezone.utc),
            ai_model=self._ai.model,
            prompt=request.prompt,
            records_analyzed=analysis.records_analyzed,
            key_metrics=analysis.key_metrics,
            tables=((plan_tables + list(chosen_tables.values())) if plan is not None and plan.lookup
                    else (list(chosen_tables.values()) + plan_tables))[:15],
            metrics=analysis.metrics,
            content=content,
            disclaimer=DEFAULT_DISCLAIMER,
            company_name=company,
            customer_id=customer_id,
            generation={"ai_calls": (0 if narrative_source == "deterministic" else 1) + planner_calls,
                        "narrative_source": narrative_source, "notice": notice},
        )

        summary = narrative.executive_summary if narrative else StructuredSummary(paragraphs=content.executive_summary.split("\n\n"))
        if intent and intent.omit_limitations:
            limitations = []
            content.data_limitations = []
        else:
            limitations = list(dict.fromkeys(content.data_limitations if intent else content.data_limitations + analysis.metrics.get("data_quality_notes", [])))
            if plan is not None:
                # What could not be answered comes first, then what the figures cover, then how wording was read.
                extra = [f"Not available: {u.requested}. {u.reason}" for u in plan.unsupported]
                if scope and (plan.scoped or scope.get("is_partial")):
                    extra.append(scope["statement"])
                    # The statement gives the exact counts; the generic coverage note would repeat it.
                    limitations = [l for l in limitations if not l.startswith("Results cover only the supplied records")]
                extra += list(plan.notes) + plan_notes
                limitations = list(dict.fromkeys(extra + limitations))[:30]
                content.data_limitations = limitations[:15]
        cards = (display_metrics(analysis, intent) if intent else
                 [DisplayMetric(key=f"metric_{i}", label=m.label, value=m.value, unit="text")
                  for i, m in enumerate(analysis.key_metrics[:4])])
        chart_notes = []
        if chart_plan.requests:
            built = await run_in_threadpool(build_charts, definition.report_type, data, chart_plan,
                                            ai_requests=outcome.get("chart_requests"),
                                            status_scope=intent.status if intent and keep is None else None)
            charts, chart_notes = built.charts, built.notes
            if not (intent and intent.omit_limitations):
                limitations = list(dict.fromkeys(limitations + [f"{n.requested_type}: {n.reason}" for n in chart_notes]))
        else:
            charts = report_charts(analysis, intent) if intent else []
        if intent and "all" in getattr(intent, "excluded_charts", ()):
            charts = []
        elif intent and getattr(intent, "excluded_charts", ()):
            charts = [c for c in charts if (c.type or "").lower() not in intent.excluded_charts and (c.id or "").lower() not in intent.excluded_charts]
        elif chart_plan.excluded and not chart_plan.requests:
            charts = [c for c in charts if (c.type or "").lower() not in chart_plan.excluded]
        if intent:
            report.key_metrics = [KeyMetric(label=m.label, value=str(m.value) + ("%" if m.unit == "percent" else ""))
                                  for m in cards]
        report.structured_report = StructuredReport(report_id=report.report_id, report_type=report.report_type,
            title=content.title, subtitle=report.report_name, generated_at=report.generated_at,
            executive_summary=summary, metrics=cards,
            sections=display_sections, findings=content.findings, recommendations=content.recommendations,
            limitations=limitations, charts=charts, chart_notes=chart_notes, scope={**analysis.metrics.get("scope", {}), **(scope or {})},
            company_name=company, customer_id=customer_id)
        if insights:
            insights["meta"] = {"narrative_source": narrative_source, "fallback_used": narrative_source == "deterministic_fallback",
                                "ai_model": self._ai.model, "generated_at": report.generated_at.isoformat()}
            report.insights = insights
            cards = headline_cards(insights)
            insights["kpi_cards"] = cards
            if insights["scope"]["single_status"]:
                report.key_metrics = [KeyMetric(label=c["label"], value=str(c["value"]), note=c.get("note")) for c in cards]
                report.structured_report.metrics = [DisplayMetric(key=c["key"], label=c["label"], value=c["value"],
                                                                  unit=c.get("unit", "text"), note=c.get("note"),
                                                                  tone=c.get("tone"), icon=c.get("icon"))
                                                    for c in cards]
        if plan is not None:
            self._verify(report, plan, scoped_rows, analysis, chart_adapter, vocab, narrative_source, claims_removed,
                         [r.type for r in chart_plan.requests])
        if source is not None:
            # Coverage (counts, filters, verification) is audit data, written to the backend log and not shown in
            # the report. Only a partial report (allow_partial) is labelled, so it never reads as complete.
            write_coverage_audit(_coverage_record(request, user_id, "generated", report_id=report.report_id,
                                                  completeness=source.completeness, notes=source.limitations))
            partial = list(source.limitations)
            structured = report.structured_report
            structured.limitations = list(dict.fromkeys(partial + structured.limitations))[:30]
        if request.previous_report:
            report = self._merge_with_previous_report(report, request.previous_report, request.prompt, intent=intent,
                                                      chart_edit=chart_edit, customer_id=request.customer_id)
        if snapshot is not None:
            if selected is not None:
                period = selected.to_dict()
                display_scope = report.structured_report.scope
                display_scope["selected_period"] = period
                if period["from_date"] and period["to_date"]:
                    from app.report_period import period_label
                    display_scope["observed_data_period"] = display_scope.get("supplied_data_period")
                    display_scope["display_period"] = period
                    display_scope["requested_period"] = {"from": period["from_date"], "to": period["to_date"],
                        "date_field": period["date_field"], "timezone": period["timezone"], "basis": _period_basis()}
                    display_scope["period_label"] = period_label(period)
                    display_scope.update(_date_range_scope())
                if report.insights:
                    report.insights["scope"]["from_date"] = period["from_date"]
                    report.insights["scope"]["to_date"] = period["to_date"]
            from app.analysis.accuracy import repair_coverage
            repair_coverage(report, snapshot, plan, chart_adapter)
            report = await run_in_threadpool(validate_final, report, snapshot, plan=plan, adapter=chart_adapter,
                                            normalized_rows=chart_adapter.rows(json.loads(snapshot.data_json)),
                                            analysis_rows=scoped_rows, block_on_failure=self._settings.report_accuracy_strict)
        return report

    def _push_source_dates(self, request, plan):
        """Push only shared date predicates into the authorized API window.

        An API response need not repeat manifest_datetime. A backend-owned date query
        already applies that predicate; do not attempt to reapply it to absent fields.
        Section-local dates stay local so comparisons and denominators remain intact.
        """
        from datetime import date
        start, end = request.filters.from_date, request.filters.to_date
        applied, local = [], []
        for condition in plan.filters:
            if condition.field != request.filters.date_field or condition.op not in {"eq", "between", "gte", "gt", "lte", "lt"}:
                local.append(condition)
                continue
            values = condition.value if isinstance(condition.value, (list, tuple)) else [condition.value]
            bounds = [date.fromisoformat(v) for v in values]
            if condition.op in {"between", "eq", "gte", "gt"}:
                lower = bounds[0] + (timedelta(days=1) if condition.op == "gt" else timedelta())
                start = max(start, lower)
            if condition.op in {"between", "eq", "lte", "lt"}:
                upper = bounds[-1] - (timedelta(days=1) if condition.op == "lt" else timedelta())
                end = min(end, upper)
            applied.append({"field": condition.field, "op": condition.op, "value": list(values) if condition.op == "between" else values[0], "applied_by": "shipment_api"})
        if start > end:
            raise NoMatchingRecordsError("No data available for the requested date range.", details={"matching_records": 0})
        from app.analysis.executor import _matches
        adapter = get_adapter("shipment")
        source_field = request.filters.date_field
        sections = []
        for section in plan.sections:
            # A section predicate already guaranteed by the complete source window
            # does not require a second copy of that date in each returned row.
            section_filters = tuple(f for f in section.filters if not (
                f.field == source_field and f.op in {"eq", "between", "gte", "gt", "lte", "lt"}
                and _matches(start, f) and _matches(end, f)))
            dependent_fields = set(section.dimensions) | {f.field for f in section_filters}
            dependent_fields.update(adapter.measures[m].field for m in section.metrics if adapter.measures[m].field)
            dependent_fields.update(field for m in section.metrics for field, _ in adapter.measures[m].where)
            required = tuple(f for f in section.required_fields if f != source_field or f in dependent_fields)
            sections.append(section if section_filters == section.filters and required == section.required_fields else
                section.model_copy(update={"filters": section_filters, "required_fields": required}))
        if not applied and all(new is old for new, old in zip(sections, plan.sections)):
            return request, plan, [], None
        authorized = selected_filters(request.filters, request.data or {}).to_dict()
        filters = request.filters.model_copy(update={"from_date": start, "to_date": end, "range_days": None, "date_range": None})
        note = f"{request.filters.date_field.replace('_', ' ').title()} filter applied by the shipment API: {start.isoformat()} to {end.isoformat()} ({request.filters.timezone})."
        return request.model_copy(update={"filters": filters}), replace(plan, filters=tuple(local), sections=tuple(sections),
            notes=(*plan.notes, note)), applied, authorized

    async def _fetch_source(self, request: ReportRequest, credential: str | None):
        """Fetch every shipment matching the selected filters and pass it through the completeness gate.

        Runs after the metadata-only plan is accepted and before metric/narrative computation.
        Completeness is always enforced here: the backend owns the fetch, so a gap is an upstream failure.
        """
        query = build_query(request.filters, self._settings)
        logger.info("Shipment range: request_id=%s report_id=%s field=%s timezone=%s selected=%s..%s "
                    "days=%s from_date=%s to_date_exclusive=%s", request_id_var.get(), report_id_var.get(),
                    query.date_filter_type, query.timezone, request.filters.from_date, request.filters.to_date,
                    (request.filters.to_date - request.filters.from_date).days + 1, query.from_date, query.to_date)
        result = await self._source.fetch(query, delegated_credential(credential))
        from app.security.privacy import register_source_identities
        register_source_identities(result.rows)
        filters = request.filters
        selected = SelectedFilters(from_date=filters.from_date, to_date=filters.to_date, status_label=query.status_label,
                                   date_field=filters.date_field, timezone=filters.timezone, pincodes=tuple(filters.pincodes))
        today = datetime.now(ZoneInfo(filters.timezone)).date().isoformat()
        data: dict[str, Any] = {"getOrderList": {"data": result.rows, "total": result.total}, "as_of": today}
        if result.account_summary:
            data["account_summary"] = result.account_summary
        data = with_report_filter(data, selected)
        # as_of above is today's date for ages and relative periods, not a source snapshot.
        info = {**result.source_info(), "data_as_of": None, "reference_date": today}
        source = await run_in_threadpool(check_source, data, selected, expected_records=result.total,
                                         allow_partial=request.allow_partial, enforce=True, rows_reconciled=True,
                                         source_owned=True, source_info=info)
        return request.model_copy(update={"data": source.data}), source, selected

    def _patch_charts(self, prev: dict[str, Any], edit: ChartEdit) -> ReportResponse | None:
        """The current report with only the edited charts changed: same report ID, KPIs, tables and text.

        None when the posted report cannot be read as a report; the caller then merges instead.
        """
        try:
            report = ReportResponse.model_validate(prev)
        except ValidationError:
            logger.warning("Posted previous report is not a valid report; applying the chart edit through a merge.")
            return None
        structured = report.structured_report
        if structured is None:
            return None
        if report.report_type == "shipment":
            receipt = verify_revision(report)
        charts = apply_chart_edit([c.model_dump() for c in structured.charts], edit)
        structured.charts = [ReportChart.model_validate(c) for c in charts]
        if isinstance(structured.verification, dict) and edit.removed:
            removed = {f"chart:{chart_id}" for chart_id in edit.removed}
            checks = [c for c in structured.verification.get("checks") or [] if c.get("check") not in removed]
            structured.verification = {
                **structured.verification, "checks": checks,
                "checked": sum(1 for c in checks if c.get("status") != "not_verifiable"),
                "not_independently_verified": [n for n in structured.verification.get("not_independently_verified") or []
                                               if n not in removed]}
        report.generation = {**(report.generation or {}), "ai_calls": 0, "chart_edit": {
            "removed": edit.removed, "retyped": edit.retyped}, "notice": edit.message}
        if report.report_type == "shipment":
            # A pure edit changes chart presentation only; data and values remain those signed above.
            from types import SimpleNamespace
            seal(report, SimpleNamespace(fingerprint=receipt["dataset"], request_id=request_id_var.get() or receipt["request_id"]))
        logger.info("Chart edit applied without regeneration: removed=%s retyped=%s", edit.removed, edit.retyped)
        return report

    async def _generate_section_report(self, request, plan, scope, rows, snapshot, source, selected, adapter, planner_calls):
        """Render the approved AnalysisPlan sections; generic templates never enter this path."""
        from app.analysis.executor import execute_ordered_sections
        from app.analysis.section_reports import narrative_messages, grounded_text, verify_section_coverage, section_title, card_label
        datasets = await run_in_threadpool(execute_ordered_sections, plan, rows)
        if not any(value is not None for dataset in datasets for key, value in dataset.metrics.items() if ".total." in key):
            from app.analysis.section_reports import _required_fields
            missing = sorted({field for spec in plan.sections for field in _required_fields(spec, adapter)
                              if not any(row.get(field) not in (None, "") for row in rows)})
            labels = ", ".join(adapter.fields[field].label for field in missing)
            raise NoMatchingRecordsError(
                f"{len(rows):,} shipments were received, but the requested analysis data is unavailable"
                + (f": {labels}." if labels else ".") + " No report was created.",
                details={"reason": "required_fields_missing" if missing else "analysis_data_unavailable",
                         "source_records": len(rows), "missing_fields": missing, "matching_records": 0,
                         "selected_filters": selected.to_dict() if selected else None})
        texts = {d.requirement.id: [d.text] for d in datasets}
        narrative_source = "deterministic_fallback"
        report_stage_var.set("narrative")
        try:
            raw = await self._ai.generate_json(narrative_messages(request.prompt, plan, datasets, scope),
                                                max_tokens=self._settings.deepseek_max_output_tokens)
            texts, repaired = grounded_text(raw, datasets)
            narrative_source = "ai_repaired" if repaired else "ai"
        except AIPrivacyError:
            raise
        except (AppError, ValidationError, ValueError, TypeError):
            logger.warning("Grounded narrative unavailable; preserving the approved section datasets. request_id=%s",
                           request_id_var.get())
        sections, content_sections, tables, charts, chart_notes, limitations, cards = [], [], [], [], [], [], []
        for index, dataset in enumerate(datasets):
            identifier = dataset.requirement.id
            # Zero/unavailable facts are always Python-authored, regardless of the AI prose.
            paragraphs = list(dict.fromkeys([dataset.text, *texts[identifier]]))
            blocks = [ParagraphBlock(type="paragraph", text=p) for p in paragraphs]
            blocks += [TableBlock(**table.model_dump()) for table in dataset.tables]
            sections.append(DisplaySection(id=identifier, title=section_title(dataset.requirement), order=index+1, blocks=blocks))
            content_sections.append({"heading": section_title(dataset.requirement), "content": "\n\n".join(paragraphs)})
            tables.extend(dataset.tables)
            charts.extend(dataset.charts)
            chart_notes.extend(dataset.chart_notes)
            limitations.extend(dataset.limitations)
            for key, value in dataset.metrics.items():
                if ".total." in key and value is not None:
                    label = card_label(dataset.requirement.title, adapter.measures[key.split(".total.", 1)[1]].label)
                    cards.append(DisplayMetric(key=key.replace(".", "_"), label=label,
                        value=value, unit="text"))
        if source:
            limitations.extend(source.limitations)
        analysis_filters = ["Analysis filter: " + f.describe(adapter, get_vocabulary("shipment")) for f in plan.filters]
        limitations = list(dict.fromkeys([*plan.notes, *analysis_filters, *limitations]))
        from app.analysis.interpreter import concise_report_title
        title = concise_report_title(plan, adapter)
        company = request.company_name or request.data.get("company_name")
        customer_id = request.customer_id or request.data.get("customer_id")
        if selected:
            period = selected.to_dict()
            scope = {**scope, "selected_period": period}
            if period["from_date"] and period["to_date"]:
                from app.report_period import period_label
                scope = {**scope, "display_period": period, "period_label": period_label(period),
                    "observed_data_period": scope.get("supplied_data_period"),
                    "requested_period": {"from": period["from_date"], "to": period["to_date"], "date_field": period["date_field"],
                                         "timezone": period["timezone"], "basis": _period_basis()},
                    **_date_range_scope()}
        summary = "Requested analyses: " + "; ".join(section_title(d.requirement) for d in datasets) + "."
        report_id = report_id_var.get() or uuid.uuid4().hex
        generated_at = datetime.now(timezone.utc)
        report = ReportResponse(report_id=report_id, report_type="shipment", report_name=title,
            generated_at=generated_at, prompt=request.prompt, ai_model=self._ai.model, records_analyzed=len(rows),
            key_metrics=[KeyMetric(label=c.label, value=str(c.value)) for c in cards[:4]], tables=tables,
            metrics={"section_metrics": {key: value for d in datasets for key, value in d.metrics.items()}},
            content=ReportContent(title=title, executive_summary=summary, sections=content_sections,
                                  findings=[], recommendations=[], data_limitations=limitations[:15]),
            structured_report=StructuredReport(report_id=report_id, report_type="shipment", title=title,
                subtitle="Shipment Analysis Report", generated_at=generated_at, executive_summary=StructuredSummary(paragraphs=[summary]),
                metrics=cards[:4], sections=sections, findings=[], recommendations=[], limitations=limitations,
                charts=charts, chart_notes=chart_notes, scope=scope, company_name=company, customer_id=customer_id,
                plan=plan.to_dict(adapter, get_vocabulary("shipment"))),
            company_name=company, customer_id=customer_id,
            generation={"ai_calls": planner_calls+1, "narrative_source": narrative_source,
                        "notice": None, "workflow": "approved_ordered_sections"})
        await run_in_threadpool(verify_section_coverage, report, plan, snapshot, datasets)
        report = await run_in_threadpool(validate_final, report, snapshot, plan=plan, adapter=adapter,
            normalized_rows=adapter.rows(request.data), analysis_rows=rows, block_on_failure=self._settings.report_accuracy_strict)
        if source:
            write_coverage_audit(_coverage_record(request, None, "generated", report_id=report_id,
                                                 completeness=source.completeness, notes=source.limitations))
        return report

    def _pincode_scope(self, plan, request):
        """Enforce UI PIN scope independently of model output in both planning modes."""
        from app.analysis.plan import Filter
        from app.analysis.interpreter import pincode_filters
        selected = selected_filters(request.filters, request.data or {})
        predicates = []
        if selected.pincodes:
            predicates.append(Filter("destination_pincode", "in", selected.pincodes, "Selected pincodes"))
        predicates.extend(pincode_filters(request.prompt or ""))
        existing = list(plan.filters)
        for predicate in predicates:
            if not any(f.field == predicate.field and f.op == predicate.op and f.value == predicate.value for f in existing):
                existing.append(predicate)
        return replace(plan, filters=tuple(existing))

    async def _build_plan(self, request, vocab, adapter, intent, chart_plan, account_scope="payload"):
        """(plan, scope, scoped rows, kept record indexes or None, planner AI calls) for this request only."""
        rows = await run_in_threadpool(adapter.rows, request.data)
        context = await run_in_threadpool(vocab.context, request.data)
        plan = interpret(request.prompt or "", vocab=vocab, adapter=adapter, rows=rows, context=context, intent=intent,
                         chart_requested=bool(chart_plan.requests))
        plan = self._pincode_scope(plan, request)
        calls = 0
        if not plan.answerable:
            reasons = [u.reason for u in plan.unsupported] or ["The request does not name anything the data contains."]
            raise UnsupportedRequestError(
                "This request cannot be answered from the supplied data. " + " ".join(dict.fromkeys(reasons)),
                details={"unsupported": [{"requested": u.requested, "reason": u.reason, "missing": list(u.missing)}
                                         for u in plan.unsupported],
                         "available_fields": sorted({vocab.label(name, adapter) for name in adapter.fields
                                                     if any(r.get(name) is not None for r in rows)})})
        scoped, missing = matching_rows(rows, plan) if plan.scoped else (rows, Counter())
        keep = {row["_index"] for row in scoped} if plan.scoped else None
        scope = dataset_scope(context, plan, adapter, vocab, rows, scoped, missing)
        return plan, scope, scoped, keep, calls

    def _verify(self, report, plan, rows, analysis, adapter, vocab, narrative_source, claims_removed, chart_types):
        """Recount every Python figure from the scoped rows; remove any that does not match."""
        structured = report.structured_report
        verification, charts, notes, tables, cards = verify_report(
            rows=rows, analysis=analysis, plan=plan, adapter=adapter, vocab=vocab, charts=structured.charts,
            tables=report.tables, cards=structured.metrics, narrative_source=narrative_source,
            claims_removed=claims_removed)
        # Preserve the evidence for the final gate. Numerical failures block the report;
        # they must not be hidden by removing a component before the public response check.
        structured.verification = verification
        structured.plan = plan.to_dict(adapter, vocab, chart_types)

    def _merge_with_previous_report(self, report: ReportResponse, prev: dict[str, Any], prompt: str,
                                    intent: Any | None = None, chart_edit: ChartEdit | None = None,
                                    customer_id: str | None = None) -> ReportResponse:
        prev_sr = prev.get("structured_report") if isinstance(prev.get("structured_report"), dict) else {}
        # Never mix requests: previous content built from other records, or for another customer, is not merged.
        prev_fp = (prev_sr.get("scope") or {}).get("data_fingerprint") if isinstance(prev_sr.get("scope"), dict) else None
        current_fp = (report.structured_report.scope or {}).get("data_fingerprint") if report.structured_report else None
        prev_customer = prev.get("customer_id") or prev_sr.get("customer_id")
        if (prev_fp and current_fp and prev_fp != current_fp) or (
                prev_customer and customer_id and str(prev_customer) != str(customer_id)):
            logger.info("Previous report not merged: it was built from different data or for another customer.")
            if report.generation is not None:
                report.generation["notice"] = ("The previous report was built from different data, so its content was "
                                               "not carried over. This report uses only the current data.")
            return report
        try:
            prev_sections = prev_sr.get("sections") or []

            merged_sections = []
            seen_ids = set()
            order = 1

            # 1. Preserve existing sections from previous report
            for sec in prev_sections:
                if not isinstance(sec, dict):
                    continue
                sec_copy = dict(sec)
                sec_id = sec_copy.get("id") or f"section_{order}"
                seen_ids.add(sec_id)
                sec_copy["order"] = order
                order += 1
                merged_sections.append(sec_copy)

            # 2. Append newly generated sections tagged with source_prompt
            if report.structured_report and report.structured_report.sections:
                for sec in report.structured_report.sections:
                    sec_dump = sec.model_dump()
                    base_id = sec_dump.get("id") or f"section_{order}"
                    unique_id = base_id
                    suffix = 1
                    while unique_id in seen_ids:
                        unique_id = f"{base_id}_{suffix}"
                        suffix += 1
                    seen_ids.add(unique_id)
                    sec_dump["id"] = unique_id
                    sec_dump["order"] = order
                    sec_dump["source_prompt"] = prompt
                    order += 1
                    merged_sections.append(sec_dump)

            # 3. Charts: the previous charts with only the resolved edit applied (see app/charts/edit.py),
            # then charts new to this request. A removed chart is never re-added by the regenerated part.
            prev_charts = [c for c in prev_sr.get("charts") or [] if isinstance(c, dict)]
            merged_charts = apply_chart_edit(prev_charts, chart_edit) if chart_edit else prev_charts
            seen_chart_ids = {c.get("id") for c in merged_charts} | set(chart_edit.removed if chart_edit else ())
            if report.structured_report and report.structured_report.charts:
                for chart in report.structured_report.charts:
                    chart_dump = chart.model_dump()
                    if chart_dump.get("id") not in seen_chart_ids:
                        seen_chart_ids.add(chart_dump.get("id"))
                        merged_charts.append(chart_dump)
            if chart_edit and report.generation is not None:
                report.generation["chart_edit"] = {"removed": chart_edit.removed, "retyped": chart_edit.retyped}
                report.generation["notice"] = chart_edit.message

            prompt_lower = (prompt or "").lower()

            # 4. Merge tables
            prev_tables = prev.get("tables") or []
            merged_tables = [t for t in prev_tables if isinstance(t, dict)]
            if intent and intent.omit_courier_charges_table:
                merged_tables = [t for t in merged_tables if "shipping charges" not in (t.get("title") or "").lower() and "courier charges" not in (t.get("title") or "").lower()]
            if re.search(r"\b(?:remove|delete|omit|drop|hide)\s+(?:all\s+)?tables?\b|\b(no|without)\s+tables?\b|\btables?\s*(?:hata|mat|nahi|remove|delete)\b", prompt_lower):
                merged_tables = []

            is_table_removal_only = bool(re.search(r"\b(?:remove|delete|omit|drop|hide|hata|mat\s+dikh)\b.*?\btables?\b|\btables?\b.*?\b(?:remove|delete|omit|drop|hide|hata|mat\s+dikh)\b", prompt_lower)) and not bool(re.search(r"\b(?:add|show|include|create|display)\b", prompt_lower))
            seen_table_titles = {t.get("title") for t in merged_tables}
            if not is_table_removal_only:
                for table in report.tables:
                    t_dump = table.model_dump()
                    if t_dump.get("title") not in seen_table_titles:
                        seen_table_titles.add(t_dump.get("title"))
                        merged_tables.append(t_dump)

            # 5. Merge findings
            prev_findings = prev.get("content", {}).get("findings") or prev_sr.get("findings") or []
            merged_findings = [f for f in prev_findings if isinstance(f, dict)]
            seen_finding_titles = {f.get("title") for f in merged_findings}
            for f in report.content.findings:
                f_dump = f.model_dump()
                if f_dump.get("title") not in seen_finding_titles:
                    seen_finding_titles.add(f_dump.get("title"))
                    merged_findings.append(f_dump)

            # 6. Merge recommendations
            prev_recs = prev.get("content", {}).get("recommendations") or prev_sr.get("recommendations") or []
            merged_recs = [r for r in prev_recs if isinstance(r, dict)]
            seen_rec_actions = {r.get("action") for r in merged_recs}
            for r in report.content.recommendations:
                r_dump = r.model_dump()
                if r_dump.get("action") not in seen_rec_actions:
                    seen_rec_actions.add(r_dump.get("action"))
                    merged_recs.append(r_dump)

            # Apply merged content back
            report.tables = [ReportTable.model_validate(t) for t in merged_tables[:30]]
            report.content.findings = [ReportFinding.model_validate(f) for f in merged_findings[:30]]
            report.content.recommendations = [ReportRecommendation.model_validate(r) for r in merged_recs[:25]]
            if not report.company_name and prev.get("company_name"):
                report.company_name = prev.get("company_name")
            if not report.customer_id and prev.get("customer_id"):
                report.customer_id = prev.get("customer_id")
            if report.structured_report:
                report.structured_report.sections = [DisplaySection.model_validate(s) for s in merged_sections[:50]]
                report.structured_report.charts = [ReportChart.model_validate(c) for c in merged_charts[:10]]
                report.structured_report.findings = report.content.findings
                report.structured_report.recommendations = report.content.recommendations
                report.structured_report.company_name = report.company_name
                report.structured_report.customer_id = report.customer_id
        except Exception:
            logger.exception("Failed to merge report with previous report; returning new report as-is.")
        return report

    async def _generate_verified_content(self, messages, analysis, user_prompt="", *, max_tokens=None, evidence_metrics=None,
                                         outcome=None):
        """Exactly one DeepSeek call. Every completed call is billed, so structure and number
        problems are fixed locally (never by calling again); what cannot be fixed becomes a
        clear error for the frontend."""
        tables = [table.model_dump() for table in analysis.tables]
        evidence_metrics = analysis.metrics if evidence_metrics is None else evidence_metrics
        trace_id = uuid.uuid4().hex
        try:
            raw = await self._ai.generate_json(messages, **({"max_tokens": max_tokens} if max_tokens is not None else {}))
        except AIResponseError as exc:
            exc.details = {**(exc.details if isinstance(exc.details, dict) else {}), "trace_id": trace_id, "ai_calls": 1}
            raise
        if isinstance(raw, dict) and "chart_requests" in raw:
            chart_requests = raw.pop("chart_requests")  # chart plan only; validated by the chart planner
            if outcome is not None:
                outcome["chart_requests"] = chart_requests
        model, validated = self._validate_structure(raw, trace_id)
        try:
            validate_numerical_claims(validated.model_dump(), evidence_metrics, tables, user_prompt=user_prompt)
            return validated.model_dump()
        except AIResponseError as exc:
            original_rejections = exc.rejections
        rejections = safe_rejections(original_rejections)
        dumped = validated.model_dump()
        for rejection in rejections:
            if rejection["field"].startswith("sections["):
                index = int(rejection["field"].split("[", 1)[1].split("]", 1)[0])
                rejection["section_id"] = dumped["sections"][index].get("id", f"section_{index}")
            _log_validation_event({"event": "report_numeric_rejection", "trace_id": trace_id, "attempt": 1, **rejection})

        # Remove only the sentences holding unsupported numbers; numbers are never rewritten.
        repair_reason = ""
        try:
            repaired_model = model.model_validate(repair_unsupported_narrative(dumped, original_rejections))
            validate_numerical_claims(repaired_model.model_dump(), evidence_metrics, tables, user_prompt=user_prompt)
        except (NarrativeRepairError, ValidationError, AIResponseError) as repair_exc:
            repaired_model = None
            repair_reason = str(repair_exc) if isinstance(repair_exc, NarrativeRepairError) else "Repaired report failed schema or evidence validation"
        for rejection in rejections:
            _log_validation_event({"event": "report_numeric_repair", "trace_id": trace_id,
                "attempt": 1, "field": rejection["field"], "value": rejection["value"],
                "reason": rejection["reason"], "repair_status": "succeeded" if repaired_model else "failed",
                "repair_reason": repair_reason})
        if repaired_model is not None:
            if outcome is not None:
                outcome["repaired"] = True
            return repaired_model.model_dump()
        error = AIResponseError(
            "The AI report contained numbers that do not match your data, so it was not used and no report "
            "was created. Please try again, or narrow your request (for example one status or one courier).",
            code="ai_unverified_numbers",
            details={"trace_id": trace_id, "ai_calls": 1, "unsupported_value_count": len(rejections),
                     "fields": list(dict.fromkeys(r["field"] for r in rejections))[:20]})
        error.rejections = original_rejections
        raise error

    def _validate_structure(self, raw, trace_id):
        """Validate the AI JSON; on failure clean it up locally once instead of re-calling DeepSeek."""
        def parse(data):
            sections = data.get("sections", [])
            legacy = isinstance(data.get("executive_summary"), str) and isinstance(sections, list) and all(
                isinstance(section, dict) and "heading" in section and "content" in section for section in sections)
            model = ReportContent if legacy else StructuredNarrative
            return model, model.model_validate(data)

        try:
            return parse(raw)
        except ValidationError as exc:
            known_locations = {"paragraph", "bullet_list", "table_reference", "charts", "metrics"}
            def schema_names(node):
                if isinstance(node, dict):
                    known_locations.update(node.get("properties", {}))
                    for item in node.values():
                        schema_names(item)
                elif isinstance(node, list):
                    for item in node:
                        schema_names(item)
            schema_names(StructuredNarrative.model_json_schema())
            schema_names(ReportContent.model_json_schema())
            errors = [{"location": [part if isinstance(part, int) or part in known_locations else "[unknown_field]" for part in e["loc"]], "type": e["type"],
                       "message": (
                           "Invalid block type; expected paragraph, bullet_list or table_reference" if e["type"] == "union_tag_invalid" else
                           "Model semantic constraint failed" if e["type"] == "value_error" else e["msg"]
                       )}
                      for e in exc.errors(include_input=False, include_context=False)[:20]]
            for error in errors:
                _log_validation_event({"event": "report_schema_rejection", "trace_id": trace_id, "attempt": 1, **error})
            try:
                result = parse(_normalize_structure(raw))
            except ValidationError as again:
                _log_validation_event({"event": "report_schema_repair", "trace_id": trace_id, "repair_status": "failed"})
                raise AIResponseError(
                    "The AI response could not be turned into a report, so no report was created. "
                    "Please try again or simplify your request.",
                    code="ai_invalid_structure",
                    details={"trace_id": trace_id, "ai_calls": 1,
                             "fields": [".".join(str(p) for p in e["location"]) for e in errors]}) from again
            _log_validation_event({"event": "report_schema_repair", "trace_id": trace_id, "repair_status": "succeeded"})
            return result

    def store_report(self, report):
        token = secrets.token_urlsafe(32)
        with _STORE_LOCK:
            now = time.monotonic()
            for key in list(_REPORTS):
                if now - _REPORTS[key][0] > _STORE_TTL:
                    del _REPORTS[key]
            while len(_REPORTS) >= 500:
                _REPORTS.popitem(last=False)
            _REPORTS[report.report_id] = (now, token, report.model_copy(deep=True))
        return token

    def get_report(self, report_id, authorization, owner_id=None):
        """In-memory reports need their access token; saved reports are only found for their owner."""
        token = (authorization or "").removeprefix("Bearer ")
        with _STORE_LOCK:
            entry = _REPORTS.get(report_id)
            if entry:
                if time.monotonic() - entry[0] > _STORE_TTL or not secrets.compare_digest(token.encode("utf-8"), entry[1].encode("utf-8")):
                    raise HTTPException(status_code=404, detail="Report not found or access denied")
                return entry[2].model_copy(deep=True)
        if not owner_id:
            raise HTTPException(status_code=404, detail="Report not found or access denied")
        from app.storage.database import storage
        db_record = storage.get_report(report_id, owner_id=owner_id)
        if db_record and db_record.get("report"):
            return ReportResponse.model_validate(db_record["report"])
        raise HTTPException(status_code=404, detail="Report not found or access denied")

    def export_docx(self, document: ReportDocument) -> tuple[bytes, str]:
        return self._export(document, generate_docx, "docx")

    def export_pptx(self, document: ReportDocument) -> tuple[bytes, str]:
        from app.generators.pptx_generator import generate_pptx
        return self._export(document, generate_pptx, "pptx")

    def preview_pptx(self, document: ReportDocument) -> dict:
        from app.generators.presentation_layout import preview_pptx
        if document.report_type == "shipment":
            verify_revision(document)
        try:
            return preview_pptx(document)
        except Exception as exc:
            raise ReportExportError("Could not prepare the PowerPoint preview.") from exc

    def export_pdf(self, document: ReportDocument) -> tuple[bytes, str]:
        return self._export(document, generate_pdf, "pdf")

    def _export(self, document: ReportDocument, generator, extension: str) -> tuple[bytes, str]:
        if document.report_type == "shipment":
            receipt = verify_revision(document)
            logger.info("Report export revision verified: request_id=%s dataset=%s revision=%s format=%s",
                        request_id_var.get(), receipt["dataset"], receipt["revision"], extension)
        try:
            content = generator(document)
        except Exception as exc:
            logger.exception("Failed to generate %s document.", extension.upper())
            raise ReportExportError(f"Could not generate the {extension.upper()} document.") from exc

        structured = document.structured_report
        filename = export_filename(document.company_name or document.customer_id,
                                   (structured.title if structured else None) or document.content.title
                                   or document.report_name, document.generated_at, extension)
        if self._settings.save_generated_files:
            self._save_copy(content, filename)
        return content, filename

    def _save_copy(self, content: bytes, filename: str) -> None:
        # Local copies stay in the private output/ folder, which is never served over HTTP.
        try:
            output_dir: Path = self._settings.output_dir
            output_dir.mkdir(parents=True, exist_ok=True)
            stem, extension = filename.rsplit(".", 1)
            (output_dir / f"{stem}_{uuid.uuid4().hex[:8]}.{extension}").write_bytes(content)
        except OSError:
            logger.exception("Could not save a local copy of %s.", filename)
