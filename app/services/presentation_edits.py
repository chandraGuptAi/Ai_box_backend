"""Presentation edits retain verified data and never fetch shipment/channel records."""
import re
from types import SimpleNamespace

from app.analysis.accuracy import verify_revision, seal
from app.errors import ClarificationRequiredError, InvalidPromptError
from app.generators.presentation_themes import THEMES
from app.models.report_schema import ReportResponse, ReportChart, ChartDataset

COLORS = {'blue': '#2563EB', 'red': '#DC2626', 'green': '#16A34A', 'purple': '#7C3AED',
          'pink': '#DB2777', 'orange': '#EA580C', 'yellow': '#CA8A04', 'teal': '#0D9488'}


def edit(report, *, theme_id=None, accent=None, background=None, text_color=None, prompt='', mark_edit=True):
    report = ReportResponse.model_validate(report.model_dump() if hasattr(report, 'model_dump') else report)
    receipt = verify_revision(report) if report.report_type in {'shipment', 'attachment'} else None
    if not report.structured_report:
        raise InvalidPromptError('This report has no editable presentation.')
    current = dict(report.structured_report.scope.get('presentation') or {})
    query = prompt.lower()
    if prompt:
        if re.search(r'\b(theme|style|colou?rs?|backgrounds?)\b', query):
            for key in THEMES:
                if key in query:
                    theme_id = key
            if re.search(r'\bmodern\b', query):
                theme_id = 'minimal'
            if 'background' in query and re.search(r'\bblack\b', query):
                theme_id = 'dark'
            if 'background' in query and re.search(r'\bwhite\b', query):
                theme_id = 'minimal'
            match = re.search(r'#[0-9a-f]{6}\b', query)
            accent = match[0] if match else next((value for name, value in COLORS.items() if re.search(r'\b'+name+r'\b', query)), None)
            for name, pattern in [('background', r'(#[0-9a-f]{6})\s+for\s+(?:slide\s+)?backgrounds?'),
                                  ('accent', r'(#[0-9a-f]{6})\s+for\s+accents?'),
                                  ('text_color', r'(#[0-9a-f]{6})\s+for\s+text')]:
                found = re.search(pattern, query)
                if found:
                    if name == 'background': background = found[1]
                    elif name == 'accent': accent = found[1]
                    else: text_color = found[1]
            if not theme_id and not accent and not background and not text_color:
                raise ClarificationRequiredError('Choose Corporate, Minimal, Dark or Colorful, or name an accent color.')
        elif not re.search(r'\b(add|include|draw|insert|lagao|jodo)\b.*\b(chart|graph)\b|\b(chart|graph)\b.*\b(add|lagao|jodo)\b', query):
            raise InvalidPromptError('This request needs report analysis.', code='not_presentation_edit')
    if theme_id:
        if theme_id not in THEMES:
            raise InvalidPromptError('Unknown presentation theme.')
        current = {'theme_id': theme_id}
    if accent:
        if not re.fullmatch(r'#[0-9a-fA-F]{6}', accent):
            raise InvalidPromptError('Accent must be a six-digit hex color.')
        current['accent'] = accent
    for name, value in [('background', background), ('text_color', text_color)]:
        if value is not None:
            if not re.fullmatch(r'#[0-9a-fA-F]{6}', value):
                raise InvalidPromptError('Colors must use six-digit hex values.')
            current[name] = value.upper()
    if re.search(r'\b(add|include|draw|insert|lagao|jodo)\b', query) and re.search(r'\b(chart|graph)\b', query):
        candidates = []
        for table in report.tables:
            if table.values and table.column_meta:
                dimensions = [i for i, c in enumerate(table.column_meta) if c.type in {'text', 'date'}]
                measures = [i for i, c in enumerate(table.column_meta) if c.type in {'number', 'currency', 'percent'}]
                if dimensions and measures:
                    candidates.append((table, dimensions[0], measures[0]))
        matches = [candidate for candidate in candidates if any(word in query for word in re.findall(r'[a-z]{4,}', candidate[0].title.lower()) if word not in {'orders', 'table', 'report'})]
        subjects = set(re.findall(r'[a-z]{4,}', query)) - {'please', 'include', 'insert', 'draw', 'chart', 'graph', 'show', 'showing', 'report', 'this', 'existing', 'data', 'with', 'from', 'into', 'style', 'theme', 'color', 'colour', 'change', 'background', 'make', 'want', 'also', 'karo', 'lagao', 'jodo', 'doughnut', 'line', 'bar', 'corporate', 'minimal', 'dark', 'colorful'} - set(COLORS)
        if subjects and not matches:
            matches = [candidate for candidate in candidates if subjects.intersection(re.findall(r'[a-z]{4,}', ' '.join(candidate[0].columns).lower()))]
            if not matches:
                raise ClarificationRequiredError('Choose an existing table for the graph: ' + ', '.join(t.title for t, _, _ in candidates))
        choices = matches or candidates
        if len(choices) != 1:
            raise ClarificationRequiredError('Name the table to chart. Available choices: ' + ', '.join(t.title for t, _, _ in candidates))
        table, dimension, measure = choices[0]
        if any(not isinstance(row[measure], (int, float)) for row in table.values):
            raise InvalidPromptError('This table does not contain usable numeric chart values.')
        kind = next((kind for kind in ('doughnut', 'pie', 'line', 'bar') if kind in query), 'bar')
        identifier = 'presentation_' + (table.id or 'table')[:55]
        chart = ReportChart(id=identifier, title=table.title, type=kind,
            labels=[str(row[dimension]) for row in table.values],
            datasets=[ChartDataset(label=table.columns[measure], data=[row[measure] for row in table.values])])
        report.structured_report.charts = [c for c in report.structured_report.charts if c.id != identifier] + [chart]
    report.structured_report.scope['presentation'] = current
    if mark_edit:
        report.generation = {**(report.generation or {}), 'ai_calls': 0,
                             'notice': 'Presentation updated using the existing verified report data.'}
    if receipt:
        seal(report, SimpleNamespace(fingerprint=receipt['dataset'], request_id=receipt['request_id']))
    return report
