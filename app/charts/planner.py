"""Turn a natural-language prompt into chart requests, without an AI call when the request is clear.

Rules first: each clause that names a chart type becomes a ``ChartRequest`` whose fields come only
from the report type's approved vocabulary. Clauses that need interpretation ("add a donut
chart") are marked unresolved; they can be planned by DeepSeek inside the report's single call
(an optional ``chart_requests`` key, validated against the same vocabulary) or fall back to the
chart type's default. DeepSeek never supplies chart numbers.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

from app.charts.fields import DatasetAdapter
from app.charts.skills import SKILLS, ChartRequest, resolve_type

_SPLIT = re.compile(r"[,;\n]+|\.(?:\s+|$)|\s+(?:and|then|also|plus|with|aur|&)\s+"
                    r"|\s+(?=(?:show|add|remove|exchange|replace|display|include|present|draw|give|create)\b)")
_ATTACH_BACK = re.compile(r"^\s*(?:with|by|for|of|showing|per|across|on|using|in)\b")
_NEGATED = re.compile(
    r"\b(?:no|not|without|remove|removing|exclude|excluding|instead\s+of|replace|replacing|drop|skip|hide|except|"
    r"don'?t|do\s+not|hata\w*|nahi)\b(?:\s+[\w-]+){0,3}\s*$")
_TREND = re.compile(r"\btrends?\b|\bover\s+time\b|\btime[- ]?series\b")
_NARRATIVE = re.compile(r"\b(?:report|summar\w*|analy[sz]\w*|insights?|findings?|recommend\w*|explain\w*|why|document|"
                        r"overview|narrative|commentary|dashboard|kpis?|tables?|tabular)\b")
_COMPARE = re.compile(r"\b(?:vs|versus|compare\w*|comparison|split|mix|share|breakdown|distribution)\b")
MAX_CHARTS = 8


@dataclass
class ChartPlan:
    requests: list[ChartRequest] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)   # chart types the user asked to remove
    chart_only: bool = False

    @property
    def unresolved(self) -> list[ChartRequest]:
        return [r for r in self.requests if not r.resolved]


def _find_types(text: str) -> list[tuple[int, int, str]]:
    masked, found = text, []
    for skill in SKILLS.values():
        for alias in skill.aliases:
            for m in list(re.finditer(alias, masked)):
                found.append((m.start(), m.end(), skill.type))
                masked = masked[:m.start()] + " " * (m.end() - m.start()) + masked[m.end():]
    found.sort()
    # "Pie/Donut chart", "bar or column chart": alternatives for one chart, so keep the first.
    return [f for i, f in enumerate(found) if i == 0 or not re.fullmatch(r"\s*(?:/|\bor\b|\|)\s*", text[found[i - 1][1]:f[0]])]


def _spans(text: str, adapter: DatasetAdapter) -> list[tuple[int, int, str]]:
    found = [(m.start(), m.end(), name) for name, spec in adapter.fields.items() for alias in spec.aliases
             for m in re.finditer(alias, text)]
    found.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    result, end = [], -1
    for start, stop, name in found:
        if start >= end:
            result.append((start, stop, name))
            end = stop
    return result


def _parse(text: str, kind: str, adapter: DatasetAdapter, clause: str) -> ChartRequest:
    spans = _spans(text, adapter)
    by = re.search(r"\b(?:by|per|across)\b", text)
    wise = [m.start() for m in re.finditer(r"[- ]wise\b", text)]
    ordered = sorted(spans, key=lambda s: (s[1] not in wise, bool(by) and s[0] < by.start(), s[0]))
    dims = list(dict.fromkeys(n for _, _, n in ordered if adapter.fields[n].kind != "number"))  # "status by courier": courier first
    numeric = tuple(dict.fromkeys(n for _, _, n in spans if adapter.fields[n].kind == "number"))
    filters = []
    for name, values in adapter.filter_values.items():
        if name == adapter.status_field:
            continue  # status words choose the measure ("cancellations"), not a filter
        mentioned = {v for pattern, v in values.items() if re.search(pattern, text)}
        others = [d for d in dims if d != name]
        if len(mentioned) == 1 and others and not _COMPARE.search(text):
            filters.append((name, mentioned.pop()))
            dims = others
    measure = next((m.name for m in adapter.measures.values() if any(re.search(a, text) for a in m.aliases)), None)
    if measure == adapter.default_measure and kind in {"histogram", "scatter", "boxplot"}:
        measure = None
    spec = adapter.measures.get(measure or "")
    if spec and dims and any(f == dims[0] for f, _ in spec.where) and spec.agg == "count":
        measure = None  # "status breakdown (delivered, in transit ...)": status names list categories, not a filter
    window = None
    w = re.search(r"\b(?:last|past|previous)\s+(\d{1,3})\s*(days?|weeks?|months?)\b", text)
    if w:
        window = int(w[1]) * {"d": 1, "w": 7, "m": 30}[w[2][0]]
    elif re.search(r"\b(?:last|past|previous)\s+week\b", text):
        window = 7
    elif re.search(r"\b(?:last|past|previous)\s+month\b", text):
        window = 30
    gran = ("day" if re.search(r"\bdaily\b|\bday[- ]wise\b|\bper\s+day\b|\bby\s+day\b", text) else
            "week" if re.search(r"\bweekly\b|\bweek[- ]wise\b|\bper\s+week\b|\bby\s+week\b", text) else
            "month" if re.search(r"\bmonthly\b|\bmonth[- ]wise\b|\bper\s+month\b|\bby\s+month\b", text) else None)
    date_field = adapter.date_field
    if date_field and kind in {"line", "area", "bar"} and date_field not in dims and (gran or (window and not dims)):
        dims.insert(0, date_field)
    if date_field in dims and kind in {"line", "area"}:
        dims.remove(date_field)
        dims.insert(0, date_field)  # trends run over time; any category becomes the series
    top = re.search(r"\b(?:top|bottom|lowest|highest)\s+(\d{1,2})\b", text)
    sort = "asc" if re.search(r"\b(?:bottom|lowest|least|ascending)\b", text) else None
    skill = SKILLS[kind]
    resolved = bool(dims) or not skill.needs_dimension
    return ChartRequest(type=kind, dimension=dims[0] if dims else None, series=dims[1] if len(dims) > 1 else None,
                        measure=measure, numeric=numeric, filters=tuple(filters), window_days=window, granularity=gran,
                        limit=int(top[1]) if top else None, sort=sort, clause=clause.strip()[:160],
                        source="rule" if resolved else "default", resolved=resolved)


def plan_charts(prompt: str, report_type: str, adapter: DatasetAdapter | None) -> ChartPlan:
    """Rule-based chart plan for a prompt. Empty when no chart type (or trend) is requested."""
    text = (prompt or "").lower()
    plan = ChartPlan()
    if not text.strip():
        return plan
    clauses = [c for c in _SPLIT.split(text) if c and c.strip()]
    # Clauses without a chart type: one that follows a chart clause ("bar chart of volume | and delivery
    # counts") continues it; one that precedes ("show cod | and prepaid in a pie chart") leads into the
    # next chart clause. Report-level wording never becomes part of a chart, but the last clause before
    # a chart is kept as context for an otherwise unqualified chart ("... courier charges | show it in a pie").
    typed: list[list[Any]] = []          # [clause_text, [(start, end, type)], context]
    pending, context, adjacent = "", "", False
    for clause in clauses:
        if re.fullmatch(r"\W*\d*\W*", clause):
            adjacent = False
            continue                         # list numbering such as "1."
        matches = _find_types(clause)
        kept = []
        for start, end, kind in matches:
            if _NEGATED.search(clause[max(0, start - 40):start]):
                plan.excluded.append(kind)
            else:
                kept.append((start, end, kind))
        if not matches and _TREND.search(clause) and not _NARRATIVE.search(clause):
            kept = [(_TREND.search(clause).start(), _TREND.search(clause).end(), "line")]
        if kept:
            shift = len(pending) + 1 if pending else 0
            typed.append([pending + " " + clause if pending else clause, [(a + shift, b + shift, k) for a, b, k in kept],
                          context])
            pending, context, adjacent = "", "", True
        elif matches:
            pending, context, adjacent = "", "", False  # only negated chart mentions: about removing a chart
        elif _NARRATIVE.search(clause):
            pending, context, adjacent = "", clause, False
        elif typed and (adjacent or _ATTACH_BACK.search(clause)):
            typed[-1][0] += " " + clause
        else:
            pending = context = clause.strip()
    if not typed or adapter is None:
        plan.requests = [ChartRequest(type=k, clause=c, resolved=False, source="default") for c, ks, _ in typed for _, _, k in ks]
        plan.chart_only = bool(plan.requests) and not _NARRATIVE.search(text)
        return plan
    seen = set()
    for clause, kinds, context in typed:
        bounds = [0] + [(kinds[i][1] + kinds[i + 1][0]) // 2 for i in range(len(kinds) - 1)] + [len(clause)]
        for i, (_, _, kind) in enumerate(kinds):
            segment = clause[bounds[i]:bounds[i + 1]] if len(kinds) > 1 else clause
            request = _parse(segment, kind, adapter, segment)
            if not request.resolved and context:
                with_context = _parse(context + " " + segment, kind, adapter, segment)
                request = with_context if with_context.resolved else request
            key = (request.type, request.dimension, request.series, request.measure or adapter.default_measure,
                   request.numeric, request.filters,
                   request.window_days, request.granularity)
            if key in seen:
                continue
            seen.add(key)
            plan.requests.append(request)
    # "Add a donut chart ... donut chart of status": drop the unqualified duplicate of a qualified request.
    qualified = {r.type for r in plan.requests if r.resolved}
    plan.requests = [r for r in plan.requests if r.resolved or r.type not in qualified]  # engine notes any beyond MAX_CHARTS
    plan.chart_only = bool(plan.requests) and not _NARRATIVE.search(text)
    return plan


# --- Ambiguous requests: planned inside the report's single DeepSeek call --------------------

def chart_planning_instructions(plan: ChartPlan, adapter: DatasetAdapter) -> tuple[str, str] | None:
    """(system addition, user addition) asking DeepSeek for an optional `chart_requests` key."""
    if not plan.unresolved:
        return None
    vocab = {
        "type": sorted(SKILLS),
        "dimension": sorted(n for n, f in adapter.fields.items() if f.kind != "number"),
        "measure": list(adapter.measures),
        "numeric": sorted(n for n, f in adapter.fields.items() if f.kind == "number"),
        "filter_payment": sorted(set(adapter.filter_values.get("payment_mode", {}).values())),
        "sort": ["desc", "asc", "natural"],
    }
    system = ("CHART PLANNING: the user asked for charts listed in <charts_to_plan>. Add one top-level key "
              '"chart_requests": a list with one object per listed chart, in order: {"type","dimension","series"?,"measure",'
              '"numeric"?:[...],"filter_payment"?,"sort"?,"limit"?:1-20,"title"?:"short words, no digits"}. Use ONLY these '
              "values: " + json.dumps(vocab, separators=(",", ":")) + ". Never include numbers, data values or chart data: "
              "Python calculates every chart. Do not describe chart values in the narrative. ")
    user = "<charts_to_plan>" + json.dumps([{"chart": i + 1, "type": r.type, "request": r.clause}
                                            for i, r in enumerate(plan.unresolved)], ensure_ascii=False).replace("<", "\\u003c") \
        + "</charts_to_plan>"
    return system, user


_TITLE = re.compile(r"^[A-Za-z][A-Za-z &/()',:-]{2,79}$")


def merge_ai_requests(plan: ChartPlan, items: Any, adapter: DatasetAdapter) -> list[ChartRequest]:
    """Fill unresolved requests from validated AI plans. The user's chart type always wins."""
    valid = []
    for item in (items if isinstance(items, list) else [])[:12]:
        if not isinstance(item, dict):
            continue
        kind = resolve_type(item.get("type"))
        dim = item.get("dimension") if adapter.fields.get(item.get("dimension") or "") else None
        dim = dim if dim and adapter.fields[dim].kind != "number" else None
        series = item.get("series") if adapter.fields.get(item.get("series") or "") else None
        series = series if series and adapter.fields[series].kind == "category" and series != dim else None
        measure = item.get("measure") if item.get("measure") in adapter.measures else None
        numeric = tuple(n for n in (item.get("numeric") or [])[:2] if isinstance(n, str)
                        and adapter.fields.get(n) and adapter.fields[n].kind == "number") if isinstance(item.get("numeric"), list) else ()
        payment = item.get("filter_payment") if item.get("filter_payment") in adapter.filter_values.get("payment_mode", {}).values() else None
        limit = item.get("limit") if isinstance(item.get("limit"), int) and not isinstance(item.get("limit"), bool) \
            and 1 <= item["limit"] <= 20 else None
        title = item.get("title") if isinstance(item.get("title"), str) and _TITLE.match(item["title"].strip()) else None
        valid.append({"type": kind, "dimension": dim, "series": series, "measure": measure, "numeric": numeric,
                      "filters": (("payment_mode", payment),) if payment and dim != "payment_mode" else (),
                      "sort": item.get("sort") if item.get("sort") in {"desc", "asc", "natural"} else None,
                      "limit": limit, "title": title.strip() if title else None})
    result = []
    for request in plan.requests:
        if request.resolved:
            result.append(request)
            continue
        match = next((v for v in valid if v["type"] == request.type), None) or (valid[0] if valid else None)
        if match is None or not (match["dimension"] or match["measure"] or match["numeric"]):
            result.append(request)
            continue
        valid.remove(match)
        result.append(replace(request, dimension=match["dimension"] or request.dimension,
                              series=match["series"] or request.series, measure=match["measure"] or request.measure,
                              numeric=match["numeric"] or request.numeric, filters=request.filters or match["filters"],
                              sort=match["sort"] or request.sort, limit=match["limit"] or request.limit,
                              title=match["title"], source="ai_plan", resolved=True))
    return result
