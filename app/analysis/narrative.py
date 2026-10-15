"""Code-written narrative for requests Python answers alone (record lookups, empty results).

Every number is read from the scoped analysis, so no DeepSeek call is needed and nothing can be
invented. At most two short sentences, so compact layouts keep it as written.
"""
from __future__ import annotations

from typing import Any

from app.analysis.plan import AnalysisPlan
from app.analysis.vocabulary import PlanVocabulary
from app.charts.fields import DatasetAdapter


def _fit(sentences: list[str], limit: int = 350) -> list[str]:
    kept = sentences[:1]  # the scope sentence is always kept
    for sentence in sentences[1:]:
        if len(" ".join(kept + [sentence])) > limit:
            break
        kept.append(sentence)
    return kept


def plan_narrative(analysis: Any, plan: AnalysisPlan, scope: dict[str, Any], adapter: DatasetAdapter,
                   vocab: PlanVocabulary, title: str) -> dict[str, Any]:
    n, supplied = analysis.records_analyzed, scope.get("records_supplied", analysis.records_analyzed)
    parts = plan.describe_scope(adapter, vocab)
    noun = vocab.record_noun
    status = plan.single_status(vocab.status_field)
    if n == 0:
        only_status = status and len(plan.filters) == 1 and not plan.date_range
        sentences = [f"No {vocab.status_labels.get(status, status).lower()} {noun}s are available in the supplied data."
                     if only_status else f"No supplied {noun} records match {'; '.join(parts)}." if parts
                     else f"No {noun} records were supplied."]
        period = scope.get("supplied_data_period")
        if period:
            sentences.append(f"The supplied records cover order dates {period['from']} to {period['to']}.")
    else:
        sentences = [f"{n:,} of {supplied:,} supplied {noun} records match {'; '.join(parts)}." if parts
                     else f"{n:,} supplied {noun} records were analysed."]
        metrics = analysis.metrics
        reasons = metrics.get("cancellation_reasons") or {}
        financial = metrics.get("shipping_financial") or {}
        if status == "cancelled" and reasons.get("cancelled_orders"):
            with_reason = reasons.get("cancelled_orders_with_reason", 0)
            sentences.append(f"{with_reason:,} of them have a recorded cancellation reason." if with_reason
                             else "None of them has a recorded cancellation reason.")
        elif "financial" in plan.topics and financial.get("total_shipping_charges"):
            sentences.append(f"Their recorded shipping charges total {financial['total_shipping_charges']} across "
                             f"{financial['orders_with_valid_charge']:,} records with a valid charge.")
    return {"title": title[:200] or "Shipment Report",
            "executive_summary": {"heading": "Overview", "paragraphs": [" ".join(_fit(sentences))]},
            "sections": [], "findings": [], "recommendations": [], "data_limitations": []}
