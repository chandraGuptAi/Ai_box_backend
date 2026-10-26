"""PIN scope is enforced in both planners and never broadens to all shipments."""
import asyncio
import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

from app.analysis.completeness import selected_filters, with_report_filter
from app.analysis.interpreter import pincode_filters
from app.charts.fields import get_adapter
from app.config import settings
from app.errors import NoMatchingRecordsError
from app.models.report_schema import ReportFilters, ReportRequest
from app.services.report_service import ReportService
from tests.test_ordered_reports import PlannedAI, section, shipments
from tests.test_reports import FakeAI


def request(pincodes=None, prompt='Analyze shipment performance', records=None):
    rows = shipments() if records is None else records
    return ReportRequest.model_validate({'report_type': 'shipment', 'prompt': prompt,
        'data': {'orders': rows}, 'expected_records': len(rows),
        'filters': {'pincodes': pincodes or [], 'date_field': 'manifest_datetime',
                    'from_date': '2026-10-01', 'to_date': '2026-10-21'}})


@pytest.mark.parametrize('value', [['110001', 560001, '110001'], '110001', 110001])
def test_ui_pins_normalize_to_strings(value):
    normalized = ReportFilters(pincodes=value).pincodes
    assert normalized[0] == '110001'
    assert all(isinstance(pin, str) for pin in normalized)
    assert len(normalized) == len(set(normalized))


@pytest.mark.parametrize('value', [['12345'], ['1234567'], ['012345'], ['abc123'], [True], [110001.5]])
def test_invalid_ui_pins_rejected(value):
    with pytest.raises(ValidationError):
        ReportFilters(pincodes=value)


def test_legacy_scope_preserves_pincodes():
    data = {'report_filter': {'pincodes': [110001]}, 'orders': shipments()}
    selected = selected_filters(None, data)
    assert selected.pincodes == ('110001',)
    assert selected_filters(None, with_report_filter({}, selected)).pincodes == selected.pincodes


@pytest.mark.parametrize('prompt, op, values', [
    ('Report for pincode 110001', 'in', ('110001',)),
    ('Compare pin codes 110001 and 560001', 'in', ('110001', '560001')),
    ('Show pincode: 110001, 560001', 'in', ('110001', '560001')),
    ('110001 pincode ki report banao', 'in', ('110001',)),
    ('Exclude pincode 110001', 'not_in', ('110001',)),
])
def test_prompt_pin_filters(prompt, op, values):
    filters = pincode_filters(prompt)
    assert len(filters) == 1
    assert (filters[0].op, filters[0].value) == (op, values)


def test_pincode_wise_prompt_has_no_cohort():
    assert pincode_filters('Pincode-wise shipment report for invoice value above 110001') == []


@pytest.mark.parametrize('planner', [False, True])
def test_ui_pincode_filters_all_analysis_even_if_model_omits_it(planner):
    ai = PlannedAI([section('pin_performance', ('destination_pincode',), ('orders', 'delivery_rate', 'rto_rate'))]) if planner else FakeAI()
    report = asyncio.run(ReportService(ai, config=replace(settings, report_ai_planner=planner,
        deterministic_fallback=True)).generate_report(request(['110001'])))
    assert report.records_analyzed == 4
    scope = report.structured_report.scope
    assert scope['selected_period']['pincodes'] == ['110001']
    assert report.structured_report.plan['filters']
    if planner:
        table = report.tables[0]
        assert table.rows[0][0] == '110001'
        assert '560001' not in json.dumps([t.model_dump() for t in report.tables])


@pytest.mark.parametrize('planner', [False, True])
@pytest.mark.parametrize('pins, missing_data', [(['999999'], False), (['110001'], True)])
def test_missing_pincode_never_falls_back_to_all_shipments(planner, pins, missing_data):
    rows = shipments()
    if missing_data:
        for row in rows:
            row['consignee'].pop('pincode')
    ai = PlannedAI([section('pin_performance', ('courier',))]) if planner else FakeAI()
    with pytest.raises(NoMatchingRecordsError):
        asyncio.run(ReportService(ai, config=replace(settings, report_ai_planner=planner,
            deterministic_fallback=True)).generate_report(request(pins, records=rows)))


@pytest.mark.parametrize('planner', [False, True])
def test_prompt_only_pincode_filters(planner):
    ai = PlannedAI([section('pin_performance', ('courier',))]) if planner else FakeAI()
    report = asyncio.run(ReportService(ai, config=replace(settings, report_ai_planner=planner,
        deterministic_fallback=True)).generate_report(request(prompt='Analyze shipment performance for pincode 110001')))
    assert report.records_analyzed == 4


def test_normalizes_nested_and_numeric_destination_pins_without_using_pickup_pin():
    rows = get_adapter('shipment').rows({'orders': [
        {'order_id': 'a', 'consignee': {'pincode': 110001}, 'pickup_pincode': '560001'},
        {'order_id': 'b', 'shipping_address': {'pin_code': '560001'}},
        {'order_id': 'c', 'pickup_pincode': '110001'},
        {'order_id': 'd', 'pincode': 'not-a-pin'},
    ]})
    assert [row['destination_pincode'] for row in rows] == ['110001', '560001', None, None]

@pytest.mark.parametrize('planner', [False, True])
def test_pincode_wise_grouping(planner):
    ai = PlannedAI([section('pin_performance', ('destination_pincode',), ('orders',))]) if planner else FakeAI()
    report = asyncio.run(ReportService(ai, config=replace(settings, report_ai_planner=planner,
        deterministic_fallback=True)).generate_report(request(prompt='Show shipment counts by pincode in a table')))
    assert report.records_analyzed == 8
    pin_tables = [table for table in report.tables if table.columns[0] == 'Destination Pincode']
    assert pin_tables
    assert {row[0] for row in pin_tables[0].rows} == {'110001', '560001'}
    if not planner:
        assert not any(check['status'] == 'failed' for check in report.structured_report.verification['checks'])


def test_ui_and_prompt_filters_intersect_instead_of_widening():
    with pytest.raises(NoMatchingRecordsError):
        asyncio.run(ReportService(PlannedAI([section('performance', ('courier',))]),
            config=replace(settings, report_ai_planner=True)).generate_report(
                request(['110001'], prompt='Analyze shipments for pincode 560001')))


@pytest.mark.parametrize('planner', [False, True])
def test_shipment_api_fetches_complete_source_then_applies_pin(planner):
    import httpx
    from app.services.shipment_source import ShipmentSource
    from tests.test_shipment_source import Upstream
    upstream = Upstream(shipments())
    config = replace(settings, report_ai_planner=planner, deterministic_fallback=True,
                     shipment_api_base_url='https://shipments.test')
    ai = PlannedAI([section('performance', ('destination_pincode',))]) if planner else FakeAI()
    service = ReportService(ai, config=config, source=ShipmentSource(config, transport=httpx.MockTransport(upstream)))
    body = request(['110001']).model_dump(mode='json')
    body.update(source='shipment_api', data=None, expected_records=None)
    report = asyncio.run(service.generate_report(ReportRequest.model_validate(body),
        shipment_credential='Bearer synthetic-seller-token', user_id='synthetic-seller'))
    assert report.records_analyzed == 4
    assert report.structured_report.scope['selected_period']['pincodes'] == ['110001']
    assert upstream.requests
    assert all('pincodes' not in query for query in upstream.requests)
    from app.analysis.accuracy import verify_revision
    assert verify_revision(report)
    assert service.export_pdf(report)[0].startswith(b'%PDF')
    assert service.export_pptx(report)[0].startswith(b'PK')


def test_legacy_bad_pincode_is_a_request_validation_error():
    body = request().model_dump(mode='json')
    body['filters'] = None
    body['data']['report_filter'] = {'pincodes': ['bad-pin']}
    with pytest.raises(ValidationError):
        ReportRequest.model_validate(body)


def test_pin_filters_are_not_silently_ignored_for_other_report_types():
    body = request(['110001']).model_dump(mode='json')
    body['report_type'] = 'remittance'
    with pytest.raises(ValidationError):
        ReportRequest.model_validate(body)


@pytest.mark.parametrize('location', [
    {'consignee_details': {'pincode': '110001'}},
    {'consignee': {'address_details': {'postal_code': '110001'}}},
    {'receiver': {'zip_code': 110001}},
    {'ship_to': {'pin': '110001'}},
    {'destination_details': {'zip': '110001'}},
])
def test_nested_destination_pin_formats_are_supported(location):
    rows = get_adapter('shipment').rows({'orders': [{'order_id': 'nested-pin', **location,
        'pickup': {'pincode': '560001'}, 'seller': {'pincode': '560001'}}]})
    assert rows[0]['destination_pincode'] == '110001'


@pytest.mark.parametrize('location', [
    {'pickup': {'pincode': '110001'}}, {'origin': {'postal_code': '110001'}},
    {'consignee': {'return_address': {'pincode': '110001'}}},
])
def test_recipient_pin_fallback_never_uses_sender_or_return_codes(location):
    rows = get_adapter('shipment').rows({'orders': [{'order_id': 'sender-only', **location}]})
    assert rows[0]['destination_pincode'] is None


def test_missing_pin_analysis_reports_received_shipments_not_an_empty_source():
    rows = shipments()
    for row in rows:
        row['consignee'].pop('pincode')
    ai = PlannedAI([section('pin_counts', ('destination_pincode',))])
    with pytest.raises(NoMatchingRecordsError) as error:
        asyncio.run(ReportService(ai, config=replace(settings, report_ai_planner=True)).generate_report(
            request(prompt='Show shipment counts by pincode in a table', records=rows)))
    assert error.value.details['reason'] == 'required_fields_missing'
    assert error.value.details['source_records'] == 8
    assert error.value.details['missing_fields'] == ['destination_pincode']
    assert '8 shipments were received' in str(error.value)


def test_nonmatching_pin_reports_existing_source_separately():
    ai = PlannedAI([section('pin_counts', ('destination_pincode',))])
    with pytest.raises(NoMatchingRecordsError) as error:
        asyncio.run(ReportService(ai, config=replace(settings, report_ai_planner=True)).generate_report(
            request(['999999'])))
    assert error.value.details['reason'] == 'analysis_filters_no_match'
    assert error.value.details['source_records'] == 8
    assert error.value.details['pincode_records'] == 8
    assert error.value.details['matching_records'] == 0


def test_real_api_consignee_pin_takes_priority_over_generic_and_consignor_pin():
    record = {'order_id': 'api-shape', 'pincode': '560001',
              'consignor': {'country': 'India', 'state': 'GUJARAT', 'city': 'AHMEDABAD', 'pin': '380013'},
              'consignee': {'country': 'India', 'state': 'Delhi', 'city': 'Delhi', 'pin': '110001'}}
    row = get_adapter('shipment').rows({'orders': [record]})[0]
    assert row['destination_pincode'] == '110001'
    assert row['destination_city'] == 'Delhi'


def test_city_and_pin_sections_use_the_actual_api_shape():
    rows = shipments()
    for row in rows:
        row['consignee']['pin'] = row['consignee'].pop('pincode')
        row['consignor'] = {'city': 'Origin City', 'pin': '380013'}
    ai = PlannedAI([section('city_pin_volume', ('destination_city', 'destination_pincode'))])
    report = asyncio.run(ReportService(ai, config=replace(settings, report_ai_planner=True)).generate_report(
        request(prompt='Show city and pincode shipment counts', records=rows)))
    assert report.records_analyzed == 8
    assert report.tables[0].columns[:2] == ['Destination City', 'Destination Pincode']
    assert {r[1] for r in report.tables[0].rows} == {'110001', '560001'}
    assert not any('Data not provided: Destination Pincode' in text for text in report.structured_report.limitations)
