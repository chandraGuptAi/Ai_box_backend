import copy
import json
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.main import app
from app.api.report_routes import get_report_service
from app.services.attachment_client import AttachmentClient
from app.services.report_service import ReportService
from app.services.attachment_reports import AttachmentReports
from app.models.report_schema import ReportRequest
from app.analysis.accuracy import verify_revision
from app.errors import AppError

ID = 'a' * 32
USER = {'X-User-Id': 'attachment-test-user'}


def plan(source='attachment_only'):
    return {'title': 'Excel Order Summary', 'decision': {'source': source, 'reason': 'Prompt requests attachment data',
        'attachments': [{'attachment_id': ID, 'role': 'data'}], 'use_selected_filters': False},
        'analyses': [{'id': 'totals', 'title': 'Order Totals', 'attachment_id': ID, 'unit_index': 0,
                      'group_by': [], 'measures': [{'op': 'count', 'label': 'Orders'}, {'op': 'sum', 'column': 1, 'label': 'Amount'}]}],
        'excerpts': [], 'template_bindings': [{'attachment_id': ID, 'sheet': 'Orders', 'cell': 'B2',
                                              'analysis_id': 'totals', 'row': 0, 'column': 1}]}


class AI:
    model = 'fake-attachment-model'

    def __init__(self):
        self.plan = plan()
        self.messages = []

    async def generate_json(self, messages, **kwargs):
        self.messages.append(messages)
        return copy.deepcopy(self.plan)


class Internal:
    def __init__(self):
        self.calls = []

    async def request(self, method, path, owner, *, body=None, files=None, binary=False):
        self.calls.append((method, path, owner, body))
        assert owner == USER['X-User-Id']
        if path == '/prepare':
            return {}
        if path == '/planning-context':
            return {'attachments': [{'attachment_id': ID, 'filename': 'orders.xlsx'}],
                    'planning_instructions': 'Understand prompt before choosing source.', 'report_plan_schema': {}}
        if path == '/validate-report-plan':
            decision = body['plan']['decision']
            return {'plan': body['plan'], 'can_generate': True,
                    'fetch_external_data': decision['source'] in {'api_only', 'combined'}}
        if path == '/execute':
            return {'verified': True, 'limitations': [], 'excerpts': [], 'tables': [{
                'id': 'totals', 'title': 'Order Totals', 'columns': ['Orders', 'Amount'], 'values': [[3, 3600]],
                'total_groups': 1, 'truncated': False, 'source_rows': 3, 'matched_rows': 3,
                'totals': [3, 3600], 'group_columns': 0, 'currency': [None, '\u20b9'], 'chart': None,
                'source': {'attachment_id': ID, 'unit_index': 0, 'sheet': 'Orders', 'header_row': 1}}]}
        if path == '/' + ID + '/render':
            assert body['cells'][0]['value'] == 3600
            return httpx.Response(200, content=b'PK-template', headers={
                'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                'Content-Disposition': 'attachment; filename="report.xlsx"'})
        raise AssertionError('Unexpected source request: ' + path)


@pytest.fixture
def client(monkeypatch):
    internal, ai = Internal(), AI()
    async def request(_client, *args, **kwargs):
        return await internal.request(*args, **kwargs)
    monkeypatch.setattr(AttachmentClient, 'request', request)
    reports = ReportService(ai)
    async def forbidden(*args, **kwargs):
        raise AssertionError('Attachment-only must not fetch shipments/channels')
    monkeypatch.setattr(reports._source, 'fetch', forbidden)
    monkeypatch.setattr(reports._channel_source, 'fetch', forbidden)
    app.dependency_overrides[get_report_service] = lambda: reports
    with TestClient(app) as client:
        yield client, internal, ai
    app.dependency_overrides.clear()


def generate(client):
    return client.post('/api/reports/generate', headers=USER, json={
        'report_type': 'shipment', 'source': 'attachment', 'attachment_ids': [ID],
        'prompt': 'Excel ke data ka report banao',
        'filters': {'channel_id': 2, 'from_date': '2026-10-24', 'to_date': '2026-10-30'}})


def test_prompt_and_skill_precede_execution_without_shipment_token_or_api_fetch(client):
    client, internal, ai = client
    response = generate(client)
    assert response.status_code == 200, response.text
    report = response.json()
    assert report['report_type'] == 'attachment'
    assert report['records_analyzed'] == 3
    assert report['tables'][0]['values'] == [[3, 3600]]
    assert report['tables'][0]['rows'] == [['3', '\u20b93,600.00']]
    assert [path for _, path, _, _ in internal.calls] == ['/planning-context', '/validate-report-plan', '/execute']
    assert 'Understand prompt' in ai.messages[0][0]['content']
    assert ID not in json.dumps(ai.messages)
    assert 'file_a' in json.dumps(ai.messages)
    assert response.headers['X-Report-Access-Token']
    from app.models.report_schema import ReportResponse
    verify_revision(ReportResponse.model_validate(report))


def test_excel_export_retains_numbers_and_rejects_tampered_generated_values(client):
    client, _, _ = client
    report = generate(client).json()
    exported = client.post('/api/reports/export/xlsx', headers=USER, json=report)
    assert exported.status_code == 200, exported.text
    book = load_workbook(BytesIO(exported.content), data_only=True)
    assert book.worksheets[1]['B2'].value == 3600
    assert '\u20b9' in book.worksheets[1]['B2'].number_format
    report['tables'][0]['values'][0][1] = 999999
    assert client.post('/api/reports/export/xlsx', headers=USER, json=report).status_code == 409


def test_template_download_uses_generated_result_bindings_and_report_capability(client):
    client, internal, _ = client
    result = generate(client)
    report = result.json()
    response = client.get(f"/api/reports/{report['report_id']}/template/{ID}",
        headers={**USER, 'Authorization': 'Bearer ' + result.headers['X-Report-Access-Token']})
    assert response.status_code == 200, response.text
    assert response.content == b'PK-template'
    assert internal.calls[-1][1] == '/' + ID + '/render'


def test_clarification_stops_before_attachment_execution_or_external_api(client):
    client, internal, ai = client
    ai.plan['decision']['source'] = 'clarification_required'
    ai.plan['decision']['question'] = 'Should this file supply data or design?'
    response = generate(client)
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'clarification_required'
    assert '/execute' not in [path for _, path, _, _ in internal.calls]
    assert len(ai.messages) == 2


def test_uncertain_classification_recovers_without_changing_user_prompt(client, monkeypatch):
    client, internal, ai = client
    original = ai.generate_json
    calls = []
    async def generate_json(messages, **kwargs):
        calls.append(copy.deepcopy(messages))
        result = await original(messages, **kwargs)
        if len(calls) == 1:
            result['decision'].update(source='clarification_required', question='Please rephrase')
        return result
    monkeypatch.setattr(ai, 'generate_json', generate_json)
    response = generate(client)
    assert response.status_code == 200, response.text
    assert response.json()['prompt'] == 'Excel ke data ka report banao'
    assert response.json()['generation']['ai_calls'] == 2
    retry = json.loads(calls[1][-1]['content'])
    assert retry['prompt'] == 'Excel ke data ka report banao'
    assert retry['selected_controls']['channel_id'] == 2
    assert 'without changing its meaning' in retry['intent_recovery']
    assert 'design_reference' in retry['intent_recovery']
    assert sum(path == '/execute' for _, path, _, _ in internal.calls) == 1


def test_authentication_is_required_before_attachment_planning(client):
    client, internal, _ = client
    response = client.post('/api/reports/generate', json={'report_type': 'shipment', 'source': 'attachment',
                          'prompt': 'Read attachment', 'attachment_ids': [ID]})
    assert response.status_code == 401
    assert not internal.calls


def test_invalid_plan_is_repaired_once_before_any_data_execution(client, monkeypatch):
    client, internal, ai = client
    original = internal.request
    attempts = 0
    async def request(method, path, owner, **kwargs):
        nonlocal attempts
        if path == '/validate-report-plan':
            attempts += 1
            if attempts == 1:
                raise AppError('Decision must classify every supplied attachment exactly once',
                               code='attachment_processing_failed', status_code=422)
        return await original(method, path, owner, **kwargs)
    monkeypatch.setattr(internal, 'request', request)
    response = generate(client)
    assert response.status_code == 200, response.text
    assert attempts == 2
    assert response.json()['generation']['ai_calls'] == 2
    assert sum(path == '/execute' for _, path, _, _ in internal.calls) == 1
    assert 'repair_instruction' in ai.messages[-1][-1]['content']


def test_repair_failure_stops_without_source_fetch_or_infinite_retries(client, monkeypatch):
    client, internal, ai = client
    original = internal.request
    async def request(method, path, owner, **kwargs):
        if path == '/validate-report-plan':
            raise AppError('Invalid attachment plan', code='attachment_processing_failed', status_code=422)
        return await original(method, path, owner, **kwargs)
    monkeypatch.setattr(internal, 'request', request)
    response = generate(client)
    assert response.status_code == 422
    assert len(ai.messages) == 2
    assert '/execute' not in [path for _, path, _, _ in internal.calls]


@pytest.mark.parametrize('recover', [False, True])
def test_prompt_roles_are_decided_before_vision_and_unused_images_are_not_read(client, monkeypatch, recover):
    client, internal, ai = client
    image_id = 'b' * 32
    ai.plan['decision']['attachments'].append({'attachment_id': image_id, 'role': 'unused'})
    original_request = internal.request
    original_generate = ai.generate_json
    source_attempts = 0
    async def request(method, path, owner, **kwargs):
        if path == '/prepare':
            raise AssertionError('An unused image must not trigger visual preparation')
        if path == '/validate-plan':
            internal.calls.append((method, path, owner, kwargs['body']))
            return {'decision': kwargs['body']['decision']}
        response = await original_request(method, path, owner, **kwargs)
        if path == '/planning-context':
            response['decision_schema'] = {}
            response['attachments'].append({'attachment_id': image_id, 'filename': 'unused.png', 'requires_vision': True})
        return response
    async def generate_json(messages, **kwargs):
        nonlocal source_attempts
        if '[attachment_source_decision]' in messages[0]['content']:
            source_attempts += 1
            if recover and source_attempts == 1:
                return {**copy.deepcopy(ai.plan['decision']), 'source': 'clarification_required',
                        'question': 'Please rephrase'}
            return copy.deepcopy(ai.plan['decision'])
        return await original_generate(messages, **kwargs)
    monkeypatch.setattr(internal, 'request', request)
    monkeypatch.setattr(ai, 'generate_json', generate_json)
    response = client.post('/api/reports/generate', headers=USER, json={
        'report_type': 'shipment', 'source': 'attachment', 'attachment_ids': [ID, image_id],
        'prompt': 'Use only the Excel data and ignore the image'})
    assert response.status_code == 200, response.text
    assert response.json()['generation']['vision_calls'] == 0
    assert response.json()['generation']['ai_calls'] == 2 + int(recover)
    assert '/validate-plan' in [path for _, path, _, _ in internal.calls]


def test_long_document_excerpt_is_split_not_a_server_error(client, monkeypatch):
    """A quote longer than one paragraph (1200 chars) used to fail with a 500; it is split instead."""
    client, internal, ai = client
    spaced = ' '.join('N O VA / I N T E L L I G E N C E report line ' + str(i) for i in range(80))   # ~3700 chars
    huge = 'word ' * 3000                                                                        # 15000 chars
    original = internal.request

    async def request(method, path, owner, **kwargs):
        result = await original(method, path, owner, **kwargs)
        if path == '/execute':
            result['excerpts'] = [{'title': 'Cover page', 'quote': spaced, 'attachment_id': ID, 'unit_index': 5},
                                  {'title': 'Appendix', 'quote': huge, 'attachment_id': ID, 'unit_index': 6}]
        return result
    monkeypatch.setattr(internal, 'request', request)
    response = generate(client)
    assert response.status_code == 200, response.text
    sections = {s['title']: s for s in response.json()['structured_report']['sections']}
    cover = [b['text'] for b in sections['Cover page']['blocks']]
    assert all(len(t) <= 1200 for t in cover) and len(cover) > 2
    assert ' '.join(' '.join(cover[:-1]).split()) == ' '.join(spaced.split())     # nothing lost
    assert cover[-1] == 'Source: attachment aaaaaaaa, unit 6.'
    appendix = [b['text'] for b in sections['Appendix']['blocks']]
    assert all(len(t) <= 1200 for t in appendix) and 'shortened' in appendix[-1]
