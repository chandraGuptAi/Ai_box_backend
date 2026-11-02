"""AI chooses sources/operations; the attachment service executes typed Python calculations."""
from datetime import datetime, timezone
import json
import re
import uuid

from app.errors import AppError, AIResponseError, ClarificationRequiredError, InvalidReportDataError
from app.models.report_schema import (ReportResponse, ReportContent, ReportTable, TableColumn,
    StructuredReport, StructuredSummary, DisplaySection, ParagraphBlock, TableBlock,
    ReportChart, ChartDataset)
from app.services.attachment_client import AttachmentClient


def text(value):
    return str(value).replace('<', '\u2039').replace('>', '\u203a')


_PARAGRAPH_LIMIT = 1200     # ParagraphBlock.text
_MAX_EXCERPT = 7600         # ReportSection.content is at most 8000, including the source line


def _split_paragraphs(value, limit=_PARAGRAPH_LIMIT):
    """A long document quote as paragraphs within ``limit``: split at line, sentence, then word boundaries."""
    parts = []
    for line in (p.strip() for p in re.split(r'\n\s*\n|\n', value)):
        while len(line) > limit:
            cut = max(line.rfind('. ', 0, limit), line.rfind('\u0964 ', 0, limit))
            cut = cut + 1 if cut > limit // 2 else line.rfind(' ', 0, limit)
            cut = cut if cut > 0 else limit
            parts.append(line[:cut].strip())
            line = line[cut:].strip()
        if line:
            parts.append(line)
    # Join short neighbours back so a quote does not become dozens of one-line paragraphs.
    merged = []
    for part in parts:
        if merged and len(merged[-1]) + 1 + len(part) <= limit:
            merged[-1] += '\n' + part
        else:
            merged.append(part)
    return merged or ['(empty excerpt)']


INTENT_RECOVERY = (
    'Before asking the user to rephrase, improve the wording internally without changing its meaning, '
    'then classify and create the requested plan. Preserve the subject, calculations, dates, filters, '
    'output format and every data/design requirement. Do not invent facts or missing data. '
    'For example, orders in the selected date range plus "use the attached PPT style/theme" means '
    'API order data with selected controls and a design_reference attachment, not attachment data. '
    'An attachment-only data request must never become an API request. Missing optional formatting '
    'or imperfect grammar is not a reason to clarify. Exact template fidelity limitations do not prevent '
    'planning a supported report. Clarify only if essential information is genuinely absent, giving '
    'the specific missing information rather than a generic request to rephrase.'
)


class AttachmentReports:
    def __init__(self, ai, client=None):
        self.ai, self.client = ai, client or AttachmentClient()

    async def generate(self, request, reports, owner, credential):
        base = {'prompt': request.prompt, 'attachment_ids': request.attachment_ids}
        context = await self.client.request('POST', '/planning-context', owner, body=base)
        vision_calls, source_calls, approved_source = 0, 0, None
        payload = {key: value for key, value in context.items()
                   if key not in {'planning_instructions', 'decision_schema', 'report_plan_schema'}}
        payload['schema'] = context['report_plan_schema']
        payload['prompt'] = request.prompt
        payload['selected_controls'] = request.filters.model_dump(mode='json') if request.filters else None
        payload['external_capabilities'] = ['shipment reports', 'selected sales channel order reports']
        if request.previous_report:
            from app.analysis.accuracy import verify_revision
            previous = ReportResponse.model_validate(request.previous_report)
            verify_revision(previous)
            if set((previous.generation or {}).get('attachment_ids', [])) == set(request.attachment_ids):
                payload['follow_up_context'] = {'previous_prompt': previous.prompt,
                    'previous_plan': previous.structured_report.plan if previous.structured_report else None}
        # Random UUIDs can accidentally match IBAN/identifier detectors. Give the model harmless
        # request-local references instead; restore real IDs only after the outbound privacy boundary.
        aliases = {identifier: 'file_' + chr(97 + index) for index, identifier in enumerate(request.attachment_ids)}
        def references(value, mapping):
            if isinstance(value, dict):
                return {key: references(item, mapping) for key, item in value.items()}
            if isinstance(value, list):
                return [references(item, mapping) for item in value]
            return mapping.get(value, value) if isinstance(value, str) else value
        payload = references(payload, aliases)
        if any(item.get('requires_vision') and not item.get('vision_prepared') for item in context['attachments']):
            # Understand the prompt/roles first: an unused image must not trigger a paid vision call.
            source_payload = {**payload, 'schema': context['decision_schema']}
            source_messages = [
                {'role': 'system', 'content': '[attachment_source_decision] ' + context['planning_instructions']
                    + '\nFor this step return ONLY SourceDecision, not a complete report plan. Decide file roles from '
                    'the prompt and available metadata first. Unread visual files can be read in the next step; '
                    'do not require OCR text merely to decide whether the user wants the image used.'},
                {'role': 'user', 'content': json.dumps(source_payload, ensure_ascii=True)}]
            for source_calls in range(1, 3):
                source_raw = await self.ai.generate_json(source_messages, max_tokens=1800)
                source_raw = references(source_raw, {alias: identifier for identifier, alias in aliases.items()})
                source_validation = await self.client.request('POST', '/validate-plan', owner,
                                                             body={**base, 'decision': source_raw})
                approved_source = source_validation['decision']
                if approved_source['source'] != 'clarification_required':
                    break
                source_messages[-1] = {'role': 'user', 'content': json.dumps({**source_payload,
                    'intent_recovery': INTENT_RECOVERY,
                    'classification_feedback': approved_source.get('question')}, ensure_ascii=True)}
            if approved_source['source'] == 'clarification_required':
                question = approved_source.get('question') or 'How should these files be used?'
                raise ClarificationRequiredError(question, details={'questions': [question]})
            pending_visual_ids = {item['attachment_id'] for item in context['attachments']
                                  if item.get('requires_vision') and not item.get('vision_prepared')}
            used_ids = [item['attachment_id'] for item in approved_source['attachments']
                        if item['role'] != 'unused' and item['attachment_id'] in pending_visual_ids]
            if used_ids:
                prepared = await self.client.request('POST', '/prepare', owner,
                                                     body={**base, 'attachment_ids': used_ids})
                vision_calls = prepared.get('vision_calls', 0)
            context = await self.client.request('POST', '/planning-context', owner, body=base)
            payload['attachments'] = references(context['attachments'], aliases)
            payload['approved_source_decision'] = references(approved_source, aliases)
        messages = [
            {'role': 'system', 'content': '[attachment_report_plan] ' + context['planning_instructions']
                + '\n' + INTENT_RECOVERY},
            {'role': 'user', 'content': json.dumps(payload, ensure_ascii=True)}]
        for planning_calls in range(1, 3):
            raw = await self.ai.generate_json(messages, max_tokens=6000)
            if not isinstance(raw, dict):
                raise AIResponseError('The attachment report plan could not be read.')
            raw = references(raw, {alias: identifier for identifier, alias in aliases.items()})
            try:
                validated = await self.client.request('POST', '/validate-report-plan', owner, body={**base, 'plan': raw})
                if approved_source and validated['plan']['decision']['source'] != 'clarification_required':
                    chosen = validated['plan']['decision']
                    roles = lambda decision: {(item['attachment_id'], item['role']) for item in decision['attachments']}
                    if (chosen['source'] != approved_source['source'] or roles(chosen) != roles(approved_source)
                            or bool(chosen.get('use_selected_filters')) != bool(approved_source.get('use_selected_filters'))):
                        raise AppError('Preserve the approved source decision and file roles, or ask a clarification.',
                                       code='attachment_processing_failed', status_code=422)
                if validated['plan']['decision']['source'] == 'clarification_required' and planning_calls == 1:
                    messages[-1] = {'role': 'user', 'content': json.dumps({**payload,
                        'intent_recovery': INTENT_RECOVERY,
                        'classification_feedback': validated['plan']['decision'].get('question')}, ensure_ascii=True)}
                    continue
                break
            except AppError as error:
                if planning_calls == 2 or error.code != 'attachment_processing_failed' or error.status_code != 422:
                    raise
                messages[-1] = {'role': 'user', 'content': json.dumps({**payload,
                    'validation_feedback': error.message,
                    'repair_instruction': 'Return a complete plan matching the schema. Classify every supplied file ID exactly once. '
                        'Preserve every user requirement. Use data attachments only for analyses/excerpts and reference attachments '
                        'for template bindings. If a calculation is unsupported ask a clarification; do not silently drop it.'}, ensure_ascii=True)}
        plan = validated['plan']
        decision = plan['decision']
        if decision['source'] == 'clarification_required':
            raise ClarificationRequiredError(decision.get('question') or 'How should the attachment be used?',
                details={'questions': [decision.get('question') or 'Data or design reference?']})
        if not validated['can_generate']:
            raise InvalidReportDataError('The attachment data is not readable yet. Supply a readable table or configure OCR/vision.',
                                         code='attachment_unreadable')
        # Execute/validate attachment analyses before any external records are fetched.
        result = await self.client.request('POST', '/execute', owner, body={**base, 'plan': plan})
        external = None
        if validated['fetch_external_data']:
            external_source = request.external_source or ('channel_api' if request.filters and request.filters.channel_id else 'shipment_api')
            external_request = type(request).model_validate({
                **request.model_dump(), 'attachment_ids': [], 'source': external_source,
                'external_source': None, 'prompt': plan.get('external_prompt') or request.prompt})
            external = await reports._generate_report(external_request, shipment_credential=credential, user_id=owner)
        if decision['source'] == 'api_only':
            from app.analysis.accuracy import seal, verify_revision
            from types import SimpleNamespace
            receipt = verify_revision(external)
            external.prompt = request.prompt
            external.generation = {**(external.generation or {}), 'attachment_source_decision': decision,
                                   'attachment_ids': request.attachment_ids, 'vision_calls': vision_calls}
            external.generation['ai_calls'] = external.generation.get('ai_calls', 0) + planning_calls + source_calls + vision_calls
            if any(a['role'] == 'design_reference' for a in decision['attachments']):
                external.generation['notice'] = 'Attachment reference was read. Exact template export requires mapped editable placeholders.'
            seal(external, SimpleNamespace(fingerprint=receipt['dataset'], request_id=receipt['request_id']))
            return external
        report = self.build(request, plan, result, context, external)
        report.generation['vision_calls'] = vision_calls
        report.generation['ai_calls'] += vision_calls + source_calls + planning_calls - 1
        from app.analysis.accuracy import digest, seal
        from app.errors import request_id_var
        from types import SimpleNamespace
        seal(report, SimpleNamespace(fingerprint=digest(result), request_id=request_id_var.get() or uuid.uuid4().hex))
        return report

    def build(self, request, plan, result, context, external=None):
        now, identifier = datetime.now(timezone.utc), uuid.uuid4().hex
        tables, sections, content, charts, limitations = [], [], [], [], list(result['limitations'])
        records = {}
        results_by_id = {}
        for index, item in enumerate(result['tables']):
            source = item['source']
            records[(source['attachment_id'], source['unit_index'])] = item['source_rows']
            results_by_id[item['id']] = item
            group_count = item['group_columns']
            def display(value, column):
                if value is None:
                    return 'Not available'
                symbol = item['currency'][column - group_count] if column >= group_count else None
                if column >= group_count and item.get('formats', ['number'] * len(item['currency']))[column - group_count] == 'percent':
                    return f'{value:g}%'
                return f'{symbol}{value:,.2f}' if symbol and isinstance(value, (float, int)) else text(value)
            display_rows = [[display(value, column)[:500] for column, value in enumerate(row)] for row in item['values']]
            meta = [TableColumn(key=f'c{i}', label=text(label)[:100],
                    type='text' if i < group_count else 'currency' if item['currency'][i - group_count] else item.get('formats', ['number'] * len(item['currency']))[i - group_count],
                    currency=item['currency'][i - group_count] if i >= group_count else None)
                    for i, label in enumerate(item['columns'])]
            table = ReportTable(id=item['id'], title=text(item['title']), columns=[c.label for c in meta],
                rows=display_rows, values=item['values'], column_meta=meta,
                total_rows=item['total_groups'], truncated=item['truncated'],
                note=f"Source unit {source['unit_index'] + 1}; {item['matched_rows']} of {item['source_rows']} data rows matched. "
                     + ('Displayed groups are limited; totals use every matching row.' if item['truncated'] else 'Calculated from all matching rows.'))
            tables.append(table)
            paragraph = f"{item['matched_rows']} source rows match this analysis."
            sections.append(DisplaySection(id=item['id'], title=table.title, order=len(sections) + 1,
                blocks=[ParagraphBlock(type='paragraph', text=paragraph), TableBlock(**table.model_dump())]))
            content.append({'heading': table.title, 'content': paragraph})
            if item['chart'] and group_count and item['values'] and all(row[group_count] is not None for row in item['values']):
                charts.append(ReportChart(id=item['id'] + '_chart', title=table.title, type=item['chart'],
                    labels=[' / '.join(str(v) for v in row[:group_count]) for row in item['values']],
                    datasets=[ChartDataset(label=table.columns[group_count], data=[float(row[group_count]) for row in item['values']])]))
        for index, excerpt in enumerate(result['excerpts']):
            source = f"Source: attachment {excerpt['attachment_id'][:8]}, unit {excerpt['unit_index'] + 1}."
            quote = text(excerpt['quote']).strip()
            if len(quote) > _MAX_EXCERPT:
                quote = quote[:_MAX_EXCERPT].rsplit(' ', 1)[0] + ' …'
                source += ' The excerpt is shortened; the full text is in the attachment.'
            paragraphs = [*_split_paragraphs(quote), source]
            sections.append(DisplaySection(id=f'excerpt_{index}', title=text(excerpt['title']), order=len(sections) + 1,
                                          blocks=[ParagraphBlock(type='paragraph', text=p) for p in paragraphs]))
            content.append({'heading': text(excerpt['title']), 'content': '\n'.join(paragraphs)})
        if external:
            for table in external.tables:
                copy = table.model_copy(update={'id': 'external_' + (table.id or str(len(tables)))})
                tables.append(copy)
            for section in external.structured_report.sections if external.structured_report else []:
                blocks = [block.model_copy(update={'id': 'external_' + (block.id or str(len(sections)))})
                          if isinstance(block, TableBlock) else block for block in section.blocks]
                sections.append(section.model_copy(update={'id': 'external_' + section.id, 'order': len(sections) + 1, 'blocks': blocks}))
            if external.structured_report:
                charts.extend(chart.model_copy(update={'id': 'external_' + chart.id}) for chart in external.structured_report.charts)
            content.extend(external.content.sections)
            limitations.extend(external.content.data_limitations)
        if not sections:
            raise InvalidReportDataError('No usable attachment analysis was selected.', code='attachment_analysis_missing')
        if len(tables) > 15 or len(sections) > 50:
            raise InvalidReportDataError('Too many analyses for one report. Narrow the requested sections.')
        names = [a['filename'] for a in context['attachments'] if any(
            role['attachment_id'] == a['attachment_id'] and role['role'] in {'data', 'data_and_design'}
            for role in plan['decision']['attachments'])]
        summary = 'Analysis based on ' + ', '.join(text(name) for name in names) + (', together with selected API data.' if external else '.')
        template_exports = {}
        for binding in plan.get('template_bindings', []):
            if binding['attachment_id'] not in request.attachment_ids or binding['analysis_id'] not in results_by_id:
                raise InvalidReportDataError('Invalid template result reference.')
            table = next(t for t in tables if t.id == binding['analysis_id'])
            try:
                value = table.rows[binding['row']][binding['column']]
                raw_value = table.values[binding['row']][binding['column']]
            except IndexError:
                raise InvalidReportDataError('Template references an unavailable analysis result.') from None
            entry = template_exports.setdefault(binding['attachment_id'], {'replacements': {}, 'cells': []})
            if binding.get('placeholder'):
                entry['replacements'][binding['placeholder']] = value
            elif binding.get('sheet') and binding.get('cell'):
                entry['cells'].append({'sheet': binding['sheet'], 'cell': binding['cell'], 'value': raw_value})
            else:
                raise InvalidReportDataError('Template binding needs a placeholder or a worksheet cell.')
        if any(a['role'] in {'design_reference', 'data_and_design'} for a in plan['decision']['attachments']) and not template_exports:
            limitations.append('Original design was inspected. Exact template export is unavailable without mapped editable placeholders or cells.')
        count = sum(records.values()) + (external.records_analyzed if external else 0)
        if not count:
            count = len({(e['attachment_id'], e['unit_index']) for e in result['excerpts']})
        title = text(plan['title'])
        return ReportResponse(report_id=identifier, report_type='attachment', report_name=title[:120], generated_at=now,
            prompt=request.prompt, ai_model=self.ai.model, records_analyzed=count, tables=tables,
            content=ReportContent(title=title, executive_summary=summary, sections=content,
                                  data_limitations=limitations[:15]),
            structured_report=StructuredReport(report_id=identifier, report_type='attachment', title=title,
                subtitle='Attachment Analysis Report', generated_at=now, executive_summary=StructuredSummary(paragraphs=[summary]),
                metrics=[], sections=sections, findings=[], recommendations=[], limitations=limitations,
                charts=charts, scope={'source': plan['decision']['source'], 'source_label': ', '.join(names),
                    'attachment_ids': request.attachment_ids, 'record_type': 'source rows',
                    **({'external_scope': external.structured_report.scope} if external and external.structured_report else {})},
                company_name=request.company_name, customer_id=request.customer_id, plan=plan),
            company_name=request.company_name, customer_id=request.customer_id,
            metrics={'attachment_tables': result['tables']},
            generation={'ai_calls': 1 + ((external.generation or {}).get('ai_calls', 0) if external else 0),
                'narrative_source': 'verified_attachment_data', 'attachment_source_decision': plan['decision'],
                'attachment_ids': request.attachment_ids, 'template_exports': template_exports})
