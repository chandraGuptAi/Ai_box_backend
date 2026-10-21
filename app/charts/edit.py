"""Targeted chart edits on an existing report: remove or re-type specific charts, nothing else.

A follow-up such as "remove only the pie chart and keep the other graph unchanged" is resolved against
the charts the report actually contains (position, type and title words), never by regenerating the
report. Python decides exactly which chart IDs change:

- a target that matches no chart, or a singular target that matches several, raises ``ChartEditError``
  and the report is left as it was (the caller returns it unchanged to the user with the message);
- every chart is removed only when the user says so ("all charts", "both graphs", "remove the charts");
- a re-typed chart keeps its ID, labels and verified values; only ``type`` changes, and only between
  forms that can show the same data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.errors import InvalidPromptError


class ChartEditError(InvalidPromptError):
    """The chart edit could not be applied safely; nothing was changed."""
    code = "chart_edit_unresolved"


@dataclass
class ChartEdit:
    removed: list[str] = field(default_factory=list)        # chart IDs, in report order
    retyped: dict[str, str] = field(default_factory=dict)   # chart ID -> new type
    # True when the prompt asks for nothing but chart edits (and "keep the rest" remarks).
    pure: bool = False
    message: str = ""


# Types a chart can be converted to, as the user names them.
_TYPE_WORDS: list[tuple[str, str]] = [
    (r"horizontal\s+bar", "horizontal_bar"),
    (r"(?:bar|column|baar)", "bar"),
    (r"line", "line"),
    (r"area", "area"),
    (r"pie", "pie"),
    (r"(?:donut|doughnut|ring)", "doughnut"),
    (r"scatter", "scatter"),
    (r"bubble", "bubble"),
    (r"histogram", "histogram"),
    (r"heat\s*map", "heatmap"),
    (r"box\s*plot", "boxplot"),
    (r"tree\s*map", "treemap"),
    (r"funnel", "funnel"),
    (r"radar|spider", "radar"),
    (r"waterfall", "waterfall"),
    (r"gauge", "gauge"),
    (r"map", "map"),
]
_TYPE_RE = re.compile(r"\b(" + "|".join(p for p, _ in _TYPE_WORDS) + r")\b")
# Chart types a user word refers to ("bar" also covers grouped/stacked/horizontal bars).
_FAMILY = {
    "bar": {"bar", "horizontal_bar", "grouped_bar", "stacked_bar"},
    "horizontal_bar": {"horizontal_bar"},
    "pie": {"pie"},
    "doughnut": {"doughnut", "donut"},
}
_CIRCULAR = {"pie", "doughnut", "donut"}
# Charts drawn from one value per category: these can be shown in any of the convertible forms.
_CATEGORICAL = {"bar", "horizontal_bar", "grouped_bar", "stacked_bar", "line", "area", "pie", "doughnut", "donut", "funnel"}
_CONVERTIBLE = {"bar", "horizontal_bar", "line", "area", "pie", "doughnut"}

_CHART_WORD = r"(?:charts?|graphs?|plots?|visuals?|visualizations?|diagrams?|figures?)"
_REMOVE = re.compile(
    r"\b(?:remove|removing|delete|drop|hide|omit|exclude|erase|discard|get\s+rid\s+of|take\s+(?:out|off|away)|"
    r"hata\w*|nikal\w*|htao|no\s+need\s+(?:for|of)|nahi\s+chahiye|nahi\s+chaiye|mat\s+dikh\w*|"
    r"(?:don'?t|do\s+not)\s+(?:want|need|show|include)|without|no)\b")
# Only explicit change verbs: "show courier data in a bar chart" asks for a chart, it does not re-type one.
_CHANGE = re.compile(r"\b(?:change|changing|convert|converting|switch|turn|transform|replace|badal\w*)\b")
_CHANGE_TARGET = re.compile(r"\b(?:to|into|as|with|in|me|mein|ko)\s+(?:an?\s+|the\s+)?(" + _TYPE_RE.pattern[3:-3] + r")"
                            r"(?:\s+" + _CHART_WORD + r")?\b")
_KEEP = re.compile(r"\b(?:keep|keeping|retain|leave|preserve|rakho|rakh\w*|unchanged|as\s+it\s+is|as\s+is|same|baaki|"
                   r"don'?t\s+(?:touch|change)|do\s+not\s+(?:touch|change))\b")
_ALL = re.compile(r"\b(?:all|every|both|sab\w*|saare|sare|dono)\b")
_ORDINALS = [
    (r"\b(?:first|1st|pehl[aie])\b|\b(?:chart|graph)\s*(?:no\.?\s*|number\s*|#\s*)?1\b|#1\b", 0),
    (r"\b(?:second|2nd|dusr[aie]|doosr[aie])\b|\b(?:chart|graph)\s*(?:no\.?\s*|number\s*|#\s*)?2\b|#2\b", 1),
    (r"\b(?:third|3rd|teesr[aie]|tisr[aie])\b|\b(?:chart|graph)\s*(?:no\.?\s*|number\s*|#\s*)?3\b|#3\b", 2),
    (r"\b(?:fourth|4th)\b|\b(?:chart|graph)\s*(?:no\.?\s*|number\s*|#\s*)?4\b|#4\b", 3),
    (r"\b(?:fifth|5th)\b|\b(?:chart|graph)\s*(?:no\.?\s*|number\s*|#\s*)?5\b|#5\b", 4),
    (r"\b(?:last|final|aakhri|akhri)\b", -1),
]
# Splits a prompt into clauses: punctuation, conjunctions, and before each action verb.
_SPLIT = re.compile(r"[.;!?\n,]+|\b(?:and|but|then|also|while|aur|lekin|par|phir)\b|"
                    r"\s+(?=(?:please\s+)?(?:remove|delete|drop|hide|omit|exclude|erase|discard|get\s+rid|keep|retain|"
                    r"leave|preserve|change|convert|switch|turn|replace|add|include|create|generate|give)\b)")
_FILLER = {"please", "pls", "only", "just", "the", "a", "an", "this", "that", "it", "thanks", "thank", "you", "ok",
           "okay", "now", "too", "also", "kindly", "from", "report", "in", "of", "on", "for", "is", "my", "me", "do",
           "kar", "karo", "de", "do", "na", "ji", "bhi", "sirf", "bas", "ko", "se", "ka", "ki", "ke", "hai", "and"}
# Words that never identify a chart's topic.
_STOP = _FILLER | {"chart", "graph", "plot", "visual", "visualization", "diagram", "figure", "one", "showing", "shows",
                   "show", "which", "with", "data", "type", "other", "rest", "remaining", "current", "existing",
                   "unchanged", "same", "as", "is", "are", "was", "be", "into", "to", "by", "wise", "breakdown",
                   "distribution", "about", "all", "every", "both", "want", "need", "dont", "not", "no", "get", "rid",
                   "out", "off", "away", "using", "use", "form", "version", "view", "please"}
_EDIT_WORDS = re.compile(_REMOVE.pattern + "|" + _CHANGE.pattern + "|" + _KEEP.pattern + "|" + _ALL.pattern)
# Report parts that are not charts: "remove the courier table" never touches the courier chart.
_OTHER_OBJECT = re.compile(r"\b(?:tables?|sections?|paragraphs?|summary|findings?|recommendations?|kpis?|metrics?|"
                           r"cards?|limitations?|notes?|columns?|rows?|text|insights?|scope)\b")
# Clauses that ask for new content ("add a courier table", "include delivery status breakdown").
_CONTENT_VERB = re.compile(r"\b(?:add|adding|include|including|show|display|create|generate|give|compare|analy[sz]e|"
                           r"list|explain|summari[sz]e|calculate|draw|plot|insert|append|dikha\w*|batao|bata\w*)\b")


def plan_chart_edit(prompt: str, charts: list[dict[str, Any]]) -> ChartEdit | None:
    """The chart IDs a follow-up removes or re-types, or None when the prompt edits no chart.

    Raises ChartEditError when the prompt clearly edits a chart but the target is missing, ambiguous
    or cannot be converted; the caller must then leave the report unchanged.
    """
    charts = [c for c in charts if isinstance(c, dict) and c.get("id")]
    text = _normalize(prompt)
    if not text:
        return None
    clauses = [c.strip() for c in _SPLIT.split(text) if c and c.strip()]
    edit = ChartEdit()
    content = False
    last_action: str | None = None
    pending = ""
    for clause in clauses:
        if pending:
            clause, pending = f"{pending} {clause}", ""
        kind = _classify(clause, charts)
        if kind == "continuation" and last_action and _mentions_chart(clause, charts):
            kind = last_action  # "remove the courier and | status charts"
        elif kind == "continuation" and _mentions_chart(clause, charts):
            pending = clause    # "pie chart | remove kar do": the object comes before the verb
            continue
        if kind == "remove":
            _apply_remove(clause, charts, edit)
            last_action = "remove"
        elif kind == "change":
            _apply_change(clause, charts, edit)
            last_action = "change"
        elif kind in {"keep", "filler"}:
            continue
        else:
            content = True
            last_action = None
    if pending:
        content = True  # a chart named with no action: not an edit request
    if not edit.removed and not edit.retyped:
        return None
    edit.pure = not content
    edit.message = _summary(edit, charts)
    return edit


def apply_chart_edit(charts: list[dict[str, Any]], edit: ChartEdit) -> list[dict[str, Any]]:
    """Charts after the edit: removed IDs dropped, re-typed charts changed in place, the rest untouched."""
    removed = set(edit.removed)
    result = []
    for chart in charts:
        if not isinstance(chart, dict) or chart.get("id") in removed:
            continue
        new_type = edit.retyped.get(chart.get("id"))
        result.append({**chart, "type": new_type} if new_type else chart)
    return result


# --- Clause handling --------------------------------------------------------------------------

def _normalize(prompt: str) -> str:
    text = (prompt or "").lower().replace("’", "'").replace("‘", "'")
    return re.sub(r"[“”\"]", " ", text).strip()


def _classify(clause: str, charts: list[dict[str, Any]]) -> str:
    if _CHANGE.search(clause) and _CHANGE_TARGET.search(clause) and not _REMOVE.search(clause.split(" to ")[0]):
        return "change"
    if _REMOVE.search(clause) and (re.search(r"\b" + _CHART_WORD + r"\b", clause) or _TYPE_RE.search(clause) or (
            # "remove the courier" names a chart by its topic, unless it names another kind of content.
            not _OTHER_OBJECT.search(clause) and any(_score(c, _topics(clause)) for c in charts))):
        return "remove"
    if _KEEP.search(clause):
        return "keep"
    words = [w for w in re.findall(r"[a-z0-9#']+", clause) if w not in _FILLER]
    if not words:
        return "filler"
    if _CONTENT_VERB.search(clause) or _EDIT_WORDS.search(clause):
        return "content"
    return "continuation"


def _mentions_chart(clause: str, charts: list[dict[str, Any]]) -> bool:
    if re.search(r"\b" + _CHART_WORD + r"\b", clause) or _TYPE_RE.search(clause):
        return True
    topics = _topics(clause)
    return bool(topics) and any(_score(c, topics) for c in charts)


def _apply_remove(clause: str, charts: list[dict[str, Any]], edit: ChartEdit) -> None:
    targets = _resolve(clause, charts, verb="remove")
    for chart in targets:
        if chart["id"] not in edit.removed:
            edit.removed.append(chart["id"])
        edit.retyped.pop(chart["id"], None)


def _apply_change(clause: str, charts: list[dict[str, Any]], edit: ChartEdit) -> None:
    match = list(_CHANGE_TARGET.finditer(clause))[-1]
    new_type = _type_of(match.group(1))
    target_text = clause[:match.start()]
    targets = _resolve(target_text, charts, verb="change")
    for chart in targets:
        problem = _conversion_problem(chart, new_type)
        if problem:
            raise ChartEditError(problem + " Nothing was changed.", details={"charts": _catalog(charts)})
        if chart["id"] in edit.removed:
            continue
        if (chart.get("type") or "bar") != new_type:
            edit.retyped[chart["id"]] = new_type


def _resolve(text: str, charts: list[dict[str, Any]], *, verb: str) -> list[dict[str, Any]]:
    """The charts a clause names. Plural/"all" may match several; a singular target must match one."""
    described = _describe(text)
    plural = bool(_ALL.search(text)) or bool(re.search(r"\b(?:charts|graphs|plots|visuals|visualizations|diagrams)\b", text))
    candidates = list(charts)
    if described["ordinal"] is not None:
        index = described["ordinal"]
        candidates = [charts[index]] if -len(charts) <= index < len(charts) else []
    if described["types"]:
        wanted = set().union(*(_FAMILY.get(t, {t}) for t in described["types"]))
        exact = [c for c in candidates if (c.get("type") or "bar") in wanted]
        if not exact and wanted & _CIRCULAR:
            # "the pie chart" for a doughnut (and vice versa): the same circular family.
            exact = [c for c in candidates if (c.get("type") or "") in _CIRCULAR]
        candidates = exact
    if described["topics"] and candidates:
        scored = [(c, _score(c, described["topics"])) for c in candidates]
        best = max(score for _, score in scored)
        if best:
            candidates = [c for c, score in scored if score == best]
        elif not (described["types"] or described["ordinal"] is not None):
            candidates = []
    asked = _quote(text)
    if not candidates:
        raise ChartEditError(
            f"No chart in this report matches {asked}. Charts in the report: {_listing(charts)}. Nothing was changed.",
            code="chart_not_found", details={"charts": _catalog(charts)})
    if len(candidates) > 1 and not plural:
        raise ChartEditError(
            f"{asked[0].upper() + asked[1:]} matches {len(candidates)} charts: {_listing(candidates, charts)}. "
            f"Say which one to {verb}, for example \"{verb} chart {charts.index(candidates[0]) + 1}\" or "
            f"\"{verb} the {candidates[0].get('title')} chart\". Nothing was changed.",
            code="chart_target_ambiguous", details={"charts": _catalog(candidates, charts)})
    return candidates


def _describe(text: str) -> dict[str, Any]:
    ordinal = None
    for pattern, index in _ORDINALS:
        if re.search(pattern, text):
            ordinal = index
            break
    types = [_type_of(m.group(1)) for m in _TYPE_RE.finditer(text)]
    return {"ordinal": ordinal, "types": types, "topics": _topics(text)}


def _topics(text: str) -> list[str]:
    stripped = _ALL.sub(" ", _REMOVE.sub(" ", _CHANGE.sub(" ", _TYPE_RE.sub(" ", text))))
    for pattern, _ in _ORDINALS:
        stripped = re.sub(pattern, " ", stripped)
    words = [_stem(w) for w in re.findall(r"[a-z][a-z0-9]+", stripped)]
    return [w for w in dict.fromkeys(words) if w not in _STOP and len(w) > 1]


def _score(chart: dict[str, Any], topics: list[str]) -> int:
    haystack = " ".join(str(chart.get(k) or "") for k in ("title", "id", "description")).lower().replace("_", " ")
    words = {_stem(w) for w in re.findall(r"[a-z][a-z0-9]+", haystack)}
    return sum(1 for t in topics if t in words)


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _type_of(word: str) -> str:
    for pattern, kind in _TYPE_WORDS:
        if re.fullmatch(pattern, word):
            return kind
    return word


def _conversion_problem(chart: dict[str, Any], new_type: str) -> str | None:
    current = chart.get("type") or "bar"
    title = chart.get("title") or chart.get("id")
    if new_type not in _CONVERTIBLE:
        return (f"\"{title}\" cannot be shown as a {new_type.replace('_', ' ')} chart from its data; "
                f"supported forms are bar, horizontal bar, line, area, pie and doughnut.")
    if current not in _CATEGORICAL:
        return f"\"{title}\" is a {current.replace('_', ' ')} chart and its data cannot be shown as a {new_type.replace('_', ' ')} chart."
    if new_type in _CIRCULAR:
        datasets = chart.get("datasets") or []
        if len(datasets) != 1:
            return (f"\"{title}\" compares {len(datasets)} series; a {new_type} chart can show only one, "
                    f"so it cannot be converted without dropping data.")
        if not sum(v for v in (datasets[0].get("data") or []) if isinstance(v, (int, float))):
            return f"\"{title}\" has no non-zero values, so it cannot be shown as a {new_type} chart."
    return None


def _quote(text: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9#']+", _REMOVE.sub(" ", _CHANGE.sub(" ", text)))
             if w not in _FILLER - {"the", "a", "an", "this", "that"}]
    return '"' + (" ".join(words).strip() or "that chart") + '"'


def _catalog(subset: list[dict[str, Any]], charts: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    charts = charts or subset
    return [{"position": charts.index(c) + 1, "id": c.get("id"), "title": c.get("title"), "type": c.get("type")}
            for c in subset]


def _listing(subset: list[dict[str, Any]], charts: list[dict[str, Any]] | None = None) -> str:
    return "; ".join(f"{e['position']}. {e['title']} ({(e['type'] or 'bar').replace('_', ' ')})"
                     for e in _catalog(subset, charts)) or "none"


def _summary(edit: ChartEdit, charts: list[dict[str, Any]]) -> str:
    titles = {c["id"]: c.get("title") or c["id"] for c in charts}
    parts = []
    if edit.removed:
        parts.append("Removed " + ", ".join(f'"{titles[i]}"' for i in edit.removed))
    if edit.retyped:
        parts.append("Changed " + ", ".join(f'"{titles[i]}" to a {t.replace("_", " ")} chart' for i, t in edit.retyped.items()))
    kept = len(charts) - len(edit.removed)
    return "; ".join(parts) + f". {kept} chart{'s' if kept != 1 else ''} remain{'s' if kept == 1 else ''}; nothing else was changed."
