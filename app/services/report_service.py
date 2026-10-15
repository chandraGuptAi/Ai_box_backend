"""Report orchestration: analysis -> AI narrative -> validated report -> exports."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import json
import re
from dataclasses import replace
import uuid
import secrets
import time
import threading
from collections import OrderedDict
from fastapi import HTTPException
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.config import BASE_DIR, Settings, settings
from app.errors import AIResponseError, ReportExportError, InvalidReportDataError, InvalidPromptError
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
from app.charts import build_charts, chart_planning_instructions, get_adapter, plan_charts
from app.prompts.base import build_messages
from app.prompts.report_intent import (detect_intent, scope_analysis, selected_tables, display_metrics,
                                      report_charts, deterministic_narrative, shape_narrative, relevant_metrics)
from app.prompts.registry import get_report_definition, list_report_definitions
from app.services.deepseek_service import DeepSeekService
from app.services.shipment_insights import build_insights, headline_cards, insights_narrative
from app.services.report_evidence import (NarrativeRepairError, repair_unsupported_narrative,
                                          safe_rejections, validate_numerical_claims)

logger = logging.getLogger(__name__)

_AUDIT_LOCK = threading.Lock()


def _log_validation_event(event):
    logger.warning("%s", json.dumps(event, ensure_ascii=True))
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


DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MEDIA_TYPE = "application/pdf"


# Private, bounded, process-local store. A token grants access to exactly one report.
_REPORTS = OrderedDict()
_STORE_LOCK = threading.Lock()
_STORE_TTL = 24 * 60 * 60


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


class ReportService:
    def __init__(self, ai_service: DeepSeekService, config: Settings = settings) -> None:
        self._ai = ai_service
        self._settings = config
        self._prompt_skill = prompt_skill

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

    async def generate_report(self, request: ReportRequest) -> ReportResponse:
        definition = get_report_definition(request.report_type)

        # Validate that user prompt represents a meaningful analytical request
        if request.prompt and request.prompt.strip():
            validation = self._prompt_skill.validate(request.prompt, report_type=request.report_type)
            if not validation.is_valid:
                logger.info(
                    "PromptSkill rejected invalid prompt: category=%s reason=%s prompt=%r",
                    validation.category,
                    validation.reason,
                    request.prompt[:60],
                )
                raise InvalidPromptError(validation.message, details=validation.to_dict())

        # Validation + metric calculation (raises InvalidReportDataError on bad data).
        analysis = await run_in_threadpool(definition.analyze, request.data)
        insights = None
        if request.report_type == "shipment":
            try:
                insights = await run_in_threadpool(build_insights, request.data)
            except Exception:  # never let the v3 block break the existing report
                logger.exception("Shipment insights could not be built; continuing with the v2 report.")
        intent = detect_intent(request.prompt or definition.default_prompt) if request.report_type == "shipment" else None
        if intent:
            analysis = await run_in_threadpool(scope_analysis, analysis, request.data, intent)
        chart_adapter = get_adapter(definition.report_type)
        chart_plan = plan_charts(request.prompt or "", definition.report_type, chart_adapter)
        if chart_plan.chart_only and intent:
            # Charts only: Python computes them and no narrative is needed, so DeepSeek is not called.
            intent = replace(intent, mode="graphs_only", tables=False, charts=True)
        messages = build_messages(definition, analysis, request.prompt, intent)
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
        if intent and (intent.deterministic or intent.status and not analysis.records_analyzed):
            raw_content = deterministic_narrative(analysis, intent)
            if chart_plan.chart_only:
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
        chosen_tables = selected_tables(analysis, intent) if intent else {f"table_{i}": t for i, t in enumerate(analysis.tables)}
        if intent:
            raw_content = shape_narrative(raw_content, analysis, intent)

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
                exc.details = {**(exc.details or {}), "ai_calls": 1}
                raise

        report = ReportResponse(
            report_id=uuid.uuid4().hex,
            report_type=definition.report_type,
            report_name=definition.display_name,
            generated_at=datetime.now(timezone.utc),
            ai_model=self._ai.model,
            prompt=request.prompt,
            records_analyzed=analysis.records_analyzed,
            key_metrics=analysis.key_metrics,
            tables=list(chosen_tables.values()),
            metrics=analysis.metrics,
            content=content,
            disclaimer=DEFAULT_DISCLAIMER,
            generation={"ai_calls": 0 if narrative_source == "deterministic" else 1,
                        "narrative_source": narrative_source, "notice": notice},
        )

        summary = narrative.executive_summary if narrative else StructuredSummary(paragraphs=content.executive_summary.split("\n\n"))
        if intent and intent.omit_limitations:
            limitations = []
            content.data_limitations = []
        else:
            limitations = list(dict.fromkeys(content.data_limitations if intent else content.data_limitations + analysis.metrics.get("data_quality_notes", [])))
        cards = (display_metrics(analysis, intent) if intent else
                 [DisplayMetric(key=f"metric_{i}", label=m.label, value=m.value, unit="text")
                  for i, m in enumerate(analysis.key_metrics[:4])])
        chart_notes = []
        if chart_plan.requests:
            built = await run_in_threadpool(build_charts, definition.report_type, request.data, chart_plan,
                                            ai_requests=outcome.get("chart_requests"),
                                            status_scope=intent.status if intent else None)
            charts, chart_notes = built.charts, built.notes
            if not (intent and intent.omit_limitations):
                limitations = list(dict.fromkeys(limitations + [f"{n.requested_type}: {n.reason}" for n in chart_notes]))
        else:
            charts = report_charts(analysis, intent) if intent else []
        if intent:
            report.key_metrics = [KeyMetric(label=m.label, value=str(m.value) + ("%" if m.unit == "percent" else ""))
                                  for m in cards]
        report.structured_report = StructuredReport(report_id=report.report_id, report_type=report.report_type,
            title=content.title, subtitle=report.report_name, generated_at=report.generated_at,
            executive_summary=summary, metrics=cards,
            sections=display_sections, findings=content.findings, recommendations=content.recommendations,
            limitations=limitations, charts=charts, chart_notes=chart_notes, scope=analysis.metrics.get("scope", {}))
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
        if request.previous_report:
            report = self._merge_with_previous_report(report, request.previous_report, request.prompt)
        return report

    def _merge_with_previous_report(self, report: ReportResponse, prev: dict[str, Any], prompt: str) -> ReportResponse:
        try:
            prev_sr = prev.get("structured_report") or {}
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

            # 3. Merge charts (preserve previous, append new without duplicating IDs)
            prev_charts = prev_sr.get("charts") or []
            merged_charts = [c for c in prev_charts if isinstance(c, dict)]
            seen_chart_ids = {c.get("id") for c in merged_charts}
            if report.structured_report and report.structured_report.charts:
                for chart in report.structured_report.charts:
                    chart_dump = chart.model_dump()
                    if chart_dump.get("id") not in seen_chart_ids:
                        seen_chart_ids.add(chart_dump.get("id"))
                        merged_charts.append(chart_dump)

            # 4. Merge tables
            prev_tables = prev.get("tables") or []
            merged_tables = [t for t in prev_tables if isinstance(t, dict)]
            seen_table_titles = {t.get("title") for t in merged_tables}
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
            if report.structured_report:
                report.structured_report.sections = [DisplaySection.model_validate(s) for s in merged_sections[:50]]
                report.structured_report.charts = [ReportChart.model_validate(c) for c in merged_charts[:10]]
                report.structured_report.findings = report.content.findings
                report.structured_report.recommendations = report.content.recommendations
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

    def get_report(self, report_id, authorization):
        token = (authorization or "").removeprefix("Bearer ")
        with _STORE_LOCK:
            entry = _REPORTS.get(report_id)
            if entry:
                if time.monotonic() - entry[0] > _STORE_TTL or not secrets.compare_digest(token.encode("utf-8"), entry[1].encode("utf-8")):
                    raise HTTPException(status_code=404, detail="Report not found or access denied")
                return entry[2].model_copy(deep=True)
        from app.storage.database import storage
        db_record = storage.get_report(report_id)
        if db_record and db_record.get("report"):
            return ReportResponse.model_validate(db_record["report"])
        raise HTTPException(status_code=404, detail="Report not found or access denied")

    def export_docx(self, document: ReportDocument) -> tuple[bytes, str]:
        return self._export(document, generate_docx, "docx")

    def export_pdf(self, document: ReportDocument) -> tuple[bytes, str]:
        return self._export(document, generate_pdf, "pdf")

    def _export(self, document: ReportDocument, generator, extension: str) -> tuple[bytes, str]:
        try:
            content = generator(document)
        except Exception as exc:
            logger.exception("Failed to generate %s document.", extension.upper())
            raise ReportExportError(f"Could not generate the {extension.upper()} document.") from exc

        filename = f"{document.report_type}_report_{document.generated_at:%Y%m%d_%H%M%S}.{extension}"
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
