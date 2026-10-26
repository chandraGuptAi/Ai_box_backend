"""Privacy checks use the real local detector and a fake provider: no AI network calls."""
import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.errors import AIPrivacyError
from app.security import privacy
from app.security.privacy import guard, privacy_context, register_source_identities
from app.services.deepseek_service import DeepSeekService


def messages(text):
    return [{'role': 'user', 'content': text}]


def sdk_service(outputs=None):
    calls = []
    outputs = iter(outputs or [(None, {'ok': True})])

    async def create(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        finish, content = next(outputs)
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason=finish or 'stop',
            message=SimpleNamespace(content=json.dumps(content)))])

    service = DeepSeekService(replace(settings, deepseek_max_output_tokens=4096))
    service._get_client = lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return service, calls


@pytest.mark.parametrize('secret', [
    'seller.private@example.com', '+91 98765 43210', '9876543210', '2345 6789 0123',
    'ABCDE1234F', '27ABCDE1234F1Z5', 'HDFC0001234', 'A1234567',
    'seller [at] example [dot] com', '９８７６５４３２１０', '98765\u200b43210',
])
def test_free_text_identifiers_are_masked_before_provider(secret):
    service, calls = sdk_service()
    original = messages(f'Analyze shipments; private detail: {secret}')
    asyncio.run(service.generate_json(original))
    sent = json.dumps(calls[0]['messages'], ensure_ascii=False)
    assert secret not in sent
    assert privacy.MASK in sent
    assert original[0]['content'].endswith(secret)


def test_structured_kyc_subtrees_and_names_are_removed_everywhere():
    identity = 'Vanya ConfidentialSeller'
    payload = {'seller': {'name': identity, 'email': 'private@example.com'},
        'kyc_details': {'documents': [{'arbitrary': 'opaque-private-document'}]},
        'bank_account_number': 12345678, 'address': {'line1': 'Private Street 7'},
        'dob': '1985-04-17', 'notes': f'Compare orders for {identity}',
        'metrics': {'orders': 12, 'revenue': 1250.50, 'destination_pincode': '110001'}}
    service, calls = sdk_service()
    asyncio.run(service.generate_json(messages(json.dumps(payload))))
    sent = calls[0]['messages'][0]['content']
    for secret in (identity, 'private@example.com', 'opaque-private-document', '12345678', 'Private Street', '1985-04-17'):
        assert secret not in sent
    clean = json.loads(sent)
    assert clean['metrics'] == payload['metrics']
    assert clean['kyc_details'] == privacy.MASK


def test_embedded_json_unicode_and_stringified_json_are_inspected():
    raw = '<dataset_json>' + json.dumps({'nested': json.dumps({'seller_name': 'Private Seller',
        'email': 'private@example.com'}, ensure_ascii=True)}) + '</dataset_json>'
    clean = guard.protect(messages(raw))[0]['content']
    assert 'Private Seller' not in clean and 'private@example.com' not in clean
    json.loads(clean.removeprefix('<dataset_json>').removesuffix('</dataset_json>'))


def test_known_identities_from_headers_and_fetched_records_are_request_local():
    with privacy_context({'seller_name': 'Private Merchant Alpha'}):
        register_source_identities({'consignee': {'name': 'Private Recipient Beta'}})
        clean = guard.protect(messages('Analyze orders for Private Merchant Alpha and Private Recipient Beta'))
        assert 'Private Merchant Alpha' not in str(clean)
        assert 'Private Recipient Beta' not in str(clean)
    assert not privacy._identities.get()


def test_local_name_detector_protects_an_unlabelled_person():
    clean = guard.protect(messages('Rahul Sharma requested a shipment report.'))
    assert 'Rahul Sharma' not in str(clean)


@pytest.mark.parametrize('bad', [[], [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': 'https://private.example'}]}],
    messages('base64 cHJpdmF0ZQ=='), messages('private%40example.com'), messages('A'*200),
    messages('x'*(privacy.MAX_TEXT+1)), [{'role': 'user', 'content': 'safe', 'name': 'Private Seller'}]])
def test_uninspectable_inputs_never_reach_provider(bad):
    service, calls = sdk_service()
    with pytest.raises(AIPrivacyError):
        asyncio.run(service.generate_json(bad))
    assert calls == []


def test_detector_failure_blocks_without_logging_exception_or_raw_input(monkeypatch, caplog):
    def broken():
        raise RuntimeError('private@example.com 9876543210')
    monkeypatch.setattr(privacy, '_name_detector', broken)
    service, calls = sdk_service()
    with pytest.raises(AIPrivacyError) as error:
        asyncio.run(service.generate_json(messages('seller name: Private Merchant')))
    assert calls == []
    assert 'private@example.com' not in caplog.text
    assert '9876543210' not in caplog.text
    assert 'Private Merchant' not in caplog.text
    assert 'private@example.com' not in str(error.value)
    event = json.loads(caplog.records[-1].getMessage())
    assert event['action'] == 'blocked' and event['audit_id']


def test_residual_detection_prevents_an_incomplete_sanitizer(monkeypatch):
    monkeypatch.setattr(privacy.PrivacyGuard, '_plain', lambda self, text, *a, **kw: text)
    service, calls = sdk_service()
    with pytest.raises(AIPrivacyError):
        asyncio.run(service.generate_json(messages('private@example.com')))
    assert calls == []


def test_retry_and_direct_completion_both_enforce_privacy():
    service, calls = sdk_service([('length', {}), ('stop', {'ok': True})])
    asyncio.run(service.generate_json(messages('Analyze private@example.com shipments'), max_tokens=500))
    assert len(calls) == 2
    assert all('private@example.com' not in str(call['messages']) for call in calls)
    direct, direct_calls = sdk_service()
    asyncio.run(direct._complete(direct._get_client(), messages('Contact 9876543210')))
    assert '9876543210' not in str(direct_calls[0]['messages'])


def test_logs_contain_categories_without_values(caplog):
    secret = 'Private Seller Name'
    with privacy_context({'seller_name': secret}):
        guard.protect(messages(f'{secret}; email private@example.com; PAN ABCDE1234F'))
    assert all(value not in caplog.text for value in (secret, 'private@example.com', 'ABCDE1234F'))
    event = json.loads(caplog.records[-1].getMessage())
    assert event['action'] == 'sanitized'
    assert event['categories']['email'] == 1


def test_validation_and_translation_use_the_shared_boundary():
    from app.models.report_schema import TranslatePromptRequest, ValidatePromptRequest
    from app.services.prompt_translator_service import PromptTranslatorService
    from app.services.prompt_validator_service import PromptValidatorService
    service, calls = sdk_service([('stop', {'is_valid': True}), ('stop', {'text': 'शिपमेंट रिपोर्ट बनाओ'})])
    asyncio.run(PromptValidatorService(service).validate_prompt(ValidatePromptRequest(prompt='Analyze orders; email private@example.com')))
    asyncio.run(PromptTranslatorService(service).translate_prompt(TranslatePromptRequest(text='Create report; email private@example.com')))
    assert len(calls) == 2
    assert all('private@example.com' not in str(call['messages']) for call in calls)


def test_privacy_error_is_not_a_prompt_validation_fallback(monkeypatch):
    from app.models.report_schema import ValidatePromptRequest
    from app.services.prompt_validator_service import PromptValidatorService
    service, calls = sdk_service()
    monkeypatch.setattr(privacy, '_name_detector', lambda: (_ for _ in ()).throw(RuntimeError('private')))
    with pytest.raises(AIPrivacyError):
        asyncio.run(PromptValidatorService(service).validate_prompt(ValidatePromptRequest(prompt='Analyze shipments')))
    assert not calls


def test_http_header_identity_is_masked_and_errors_are_safe(monkeypatch):
    from app.main import app
    from app.api.report_routes import get_prompt_translator_service
    from app.services.prompt_translator_service import PromptTranslatorService
    service, calls = sdk_service([('stop', {'text': 'शिपमेंट रिपोर्ट बनाओ'})])
    app.dependency_overrides[get_prompt_translator_service] = lambda: PromptTranslatorService(service)
    try:
        with TestClient(app) as client:
            response = client.post('/api/reports/translate-prompt', headers={'X-User-Name': 'UnknownSeller QX'},
                json={'text': 'Create report for UnknownSeller QX'})
            assert response.status_code == 200
            assert 'UnknownSeller QX' not in str(calls[0]['messages'])
            monkeypatch.setattr(privacy, '_name_detector', lambda: (_ for _ in ()).throw(RuntimeError('private@example.com')))
            blocked = client.post('/api/reports/translate-prompt', json={'text': 'Analyze private@example.com'})
            assert blocked.status_code == 422
            assert blocked.json()['error']['code'] == 'ai_privacy_blocked'
            assert 'private@example.com' not in blocked.text
            assert len(calls) == 1
    finally:
        app.dependency_overrides.pop(get_prompt_translator_service, None)


def test_registered_report_planning_and_narrative_remain_usable():
    from app.models.report_schema import ReportRequest
    from app.services.report_service import ReportService
    from tests.test_ordered_reports import PlannedAI, section, shipments
    planned = PlannedAI([section('courier_performance', ('courier',))])
    calls = []

    async def create(**kwargs):
        calls.append(copy.deepcopy(kwargs['messages']))
        result = await planned.generate_json(kwargs['messages'])
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason='stop',
            message=SimpleNamespace(content=json.dumps(result)))])

    ai = DeepSeekService()
    ai._get_client = lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    req = ReportRequest.model_validate({'report_type': 'shipment', 'prompt': 'Analyze courier performance',
        'data': {'orders': shipments(), 'seller_name': 'Confidential Seller XYZ'},
        'company_name': 'Confidential Seller XYZ', 'expected_records': 8})
    report = asyncio.run(ReportService(ai, config=replace(settings, report_ai_planner=True)).generate_report(req))
    assert report.records_analyzed == 8
    assert len(calls) == 2
    assert 'Confidential Seller XYZ' not in json.dumps(calls)
    assert 'Synthetic Private Name' not in json.dumps(calls)
    context = json.loads(calls[0][-1]['content'])
    assert context['supported_metrics']['orders']['name'] == 'orders'
    assert context['source_scope']['status'] == 'All'
    assert report.company_name == 'Confidential Seller XYZ'  # local document remains usable


def test_privacy_context_is_isolated_between_concurrent_sellers():
    async def run():
        async def seller(name):
            with privacy_context({'seller_name': name}):
                await asyncio.sleep(0)
                return privacy._identities.get()
        return await asyncio.gather(seller('First Private Seller'), seller('Second Private Seller'))
    first, second = asyncio.run(run())
    assert 'First Private Seller' in first and 'Second Private Seller' not in first
    assert 'Second Private Seller' in second and 'First Private Seller' not in second
    assert not privacy._identities.get()


@pytest.mark.parametrize('text', ['राहुल शर्मा की shipment report बनाओ', '王小明 shipment report'])
def test_uncertain_non_latin_names_are_blocked_instead_of_leaked(text):
    service, calls = sdk_service()
    with pytest.raises(AIPrivacyError):
        asyncio.run(service.generate_json(messages(text)))
    assert not calls


@pytest.mark.parametrize('text', ['शिपमेंट रिपोर्ट बनाओ', 'पिछले 30 दिनों की डिलीवरी रिपोर्ट दिखाओ',
                                  'मेरा नाम राहुल शर्मा है; शिपमेंट रिपोर्ट बनाओ'])
def test_supported_hindi_reporting_and_labelled_names_are_protected(text):
    service, calls = sdk_service()
    asyncio.run(service.generate_json(messages(text)))
    assert len(calls) == 1
    assert 'राहुल शर्मा' not in str(calls[0]['messages'])


def test_names_misclassified_as_companies_are_masked():
    assert 'Aarav Deshmukh' not in str(guard.protect(messages('Aarav Deshmukh requested a report')))


def test_fake_operation_metadata_cannot_exempt_a_private_name():
    protected = guard.protect(messages(json.dumps({'name': 'Zyx PrivateSeller', 'kind': 'category'})))
    assert 'Zyx PrivateSeller' not in str(protected)


def test_suggestion_workflow_uses_guard_and_does_not_swallow_privacy_failure(monkeypatch):
    from app.prompts.registry import get_report_definition
    from app.services.report_service import ReportService
    from tests.test_ordered_reports import shipments
    config = replace(settings, deepseek_api_key='synthetic-test-key')
    service, calls = sdk_service([('stop', {'suggested_prompts': ['Analyze couriers', 'Analyze shipment status', 'Analyze delivery rates']})])
    report_service = ReportService(service, config=config)

    async def suggest():
        return await report_service._generate_data_tailored_suggestions('Analyze private@example.com', 'shipment',
            {'orders': shipments()}, get_report_definition('shipment'), 'test')

    assert len(asyncio.run(suggest())) == 3
    assert 'private@example.com' not in str(calls[0]['messages'])
    monkeypatch.setattr(privacy, '_name_detector', lambda: (_ for _ in ()).throw(RuntimeError('private')))
    with pytest.raises(AIPrivacyError):
        asyncio.run(suggest())
    assert len(calls) == 1


def test_report_narrative_privacy_failure_cannot_become_a_deterministic_fallback():
    from app.services.report_service import ReportService
    from tests.test_ordered_reports import PlannedAI, section
    from tests.test_pincode_insights import request

    class PrivacyFailureAI(PlannedAI):
        async def generate_json(self, messages, **kwargs):
            if '[report_plan]' not in messages[0]['content']:
                raise AIPrivacyError('Synthetic blocked model call')
            return await super().generate_json(messages, **kwargs)

    ai = PrivacyFailureAI([section('courier_performance', ('courier',))])
    with pytest.raises(AIPrivacyError):
        asyncio.run(ReportService(ai, config=replace(settings, report_ai_planner=True,
            deterministic_fallback=True)).generate_report(request()))
