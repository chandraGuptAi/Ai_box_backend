from io import BytesIO
from types import SimpleNamespace
import base64

import pytest
from pptx import Presentation

from app.analysis.accuracy import seal, verify_revision
from app.generators.presentation_layout import build_slides, preview_pptx
from app.generators.pptx_generator import generate_pptx
from app.models.report_schema import ReportRequest
from app.services.presentation_edits import edit
from tests.test_ordered_reports import generate, section


def report():
    return generate(sections=[section('status', ('shipment_status',))])[0]


@pytest.mark.parametrize('theme', ['corporate', 'minimal', 'dark', 'colorful'])
def test_presets_change_preview_and_export_without_changing_values(theme):
    original = report()
    updated = edit(original, theme_id=theme)
    assert original.tables == updated.tables
    assert original.metrics == updated.metrics
    verify_revision(updated)
    slides = build_slides(updated)
    assert slides[0]['theme']['label'].lower() == theme
    preview = preview_pptx(updated)
    svg = base64.b64decode(preview['slides'][0]['image'].split(',')[1]).decode()
    assert slides[0]['theme']['cover'] in svg
    deck = Presentation(BytesIO(generate_pptx(updated)))
    assert str(deck.slides[0].shapes[1].fill.fore_color.rgb) == slides[0]['theme']['cover'][1:].upper()


def test_color_and_graph_edits_use_existing_signed_table_values():
    original = report()
    updated = edit(original, prompt='change color to red and add a bar graph')
    assert updated.structured_report.scope['presentation']['accent'] == '#DC2626'
    chart = next(c for c in updated.structured_report.charts if c.id.startswith('presentation_'))
    assert chart.labels == [row[0] for row in original.tables[0].values]
    assert chart.datasets[0].data == [row[1] for row in original.tables[0].values]
    assert updated.generation['ai_calls'] == 0
    verify_revision(updated)


def test_style_followup_shortcuts_all_source_fetches():
    import asyncio
    from app.services.report_service import ReportService
    from tests.test_ordered_reports import PlannedAI
    class Forbidden:
        async def fetch(self, *args, **kwargs):
            raise AssertionError('Presentation edits must not fetch records')
    service = ReportService(PlannedAI(), source=Forbidden(), channel_source=Forbidden())
    previous = report()
    request = ReportRequest(report_type='shipment', source='attachment', attachment_ids=['a'*32],
                            prompt='change theme to dark', previous_report=previous.model_dump(mode='json'))
    result = asyncio.run(service.generate_report(request, user_id='test-user'))
    assert result.structured_report.scope['presentation']['theme_id'] == 'dark'
    assert result.tables == previous.tables


def test_unavailable_graph_subject_does_not_chart_an_unrelated_table():
    from app.errors import ClarificationRequiredError
    with pytest.raises(ClarificationRequiredError):
        edit(report(), prompt='add monthly revenue graph')


def test_presentation_endpoint_returns_a_new_download_capability_without_shipment_auth():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.models.report_schema import ReportResponse
    original = report()
    with TestClient(app) as client:
        response = client.post('/api/reports/presentation/edit', headers={'X-User-Id': 'theme-test'},
                               json={'report': original.model_dump(mode='json'), 'theme_id': 'dark'})
    assert response.status_code == 200, response.text
    assert response.headers['X-Report-Access-Token']
    updated = ReportResponse.model_validate(response.json())
    assert updated.tables == original.tables
    assert updated.structured_report.scope['presentation']['theme_id'] == 'dark'
    verify_revision(updated)


def test_custom_background_accent_and_text_apply_to_preview_and_pptx():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.models.report_schema import ReportResponse
    original = report()
    with TestClient(app) as client:
        response = client.post('/api/reports/presentation/edit', headers={'X-User-Id': 'theme-test'}, json={
            'report': original.model_dump(mode='json'), 'background': '#EC00A3',
            'accent': '#9490AA', 'text_color': '#202020'})
    assert response.status_code == 200, response.text
    updated = ReportResponse.model_validate(response.json())
    assert updated.tables == original.tables
    verify_revision(updated)
    svg = base64.b64decode(preview_pptx(updated)['slides'][0]['image'].split(',')[1]).decode()
    assert all(color in svg for color in ['#EC00A3', '#9490AA', '#202020'])
    deck = Presentation(BytesIO(generate_pptx(updated)))
    assert str(deck.slides[0].shapes[1].fill.fore_color.rgb) == 'EC00A3'
    assert any(shape.has_text_frame and any(paragraph.font.color.type == 1 and str(paragraph.font.color.rgb) == '202020'
        for paragraph in shape.text_frame.paragraphs)
        for shape in deck.slides[0].shapes)


def test_previous_custom_color_sentence_remains_compatible():
    updated = edit(report(), prompt='Change only the presentation colors. Use #EC00A3 for slide backgrounds, #9490AA for accents and charts, and #202020 for text. Keep all report data and slide content unchanged.')
    settings = updated.structured_report.scope['presentation']
    assert settings['background'] == '#EC00A3'
    assert settings['accent'].upper() == '#9490AA'
    assert settings['text_color'] == '#202020'
