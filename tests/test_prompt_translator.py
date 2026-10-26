"""Tests for the Hindi question converter (/api/reports/translate-prompt)."""

import pytest
from unittest.mock import AsyncMock
from starlette.testclient import TestClient

from app.api.report_routes import get_prompt_translator_service
from app.errors import AIResponseError, AIServiceUnavailableError
from app.main import app
from app.models.report_schema import TranslatePromptRequest
from app.services.prompt_translator_service import PromptTranslatorService


def _service(result=None, error=None) -> tuple[PromptTranslatorService, AsyncMock]:
    deepseek = AsyncMock()
    if error is not None:
        deepseek.generate_json.side_effect = error
    else:
        deepseek.generate_json.return_value = result
    return PromptTranslatorService(deepseek=deepseek), deepseek


@pytest.mark.anyio
async def test_translates_hinglish_to_devanagari():
    service, deepseek = _service({"text": "पिछले महीने दिल्ली में RTO क्यों बढ़ा?"})
    res = await service.translate_prompt(
        TranslatePromptRequest(text="Pichhle mahine Delhi mein RTO kyun badha?")
    )
    assert res.text == "पिछले महीने दिल्ली में RTO क्यों बढ़ा?"
    assert res.source_language == "hinglish"
    user_message = deepseek.generate_json.call_args.args[0][1]["content"]
    assert "Pichhle mahine Delhi mein RTO kyun badha?" in user_message


@pytest.mark.anyio
async def test_hindi_input_is_returned_without_calling_the_ai():
    service, deepseek = _service()
    res = await service.translate_prompt(TranslatePromptRequest(text="RTO क्यों बढ़ा?"))
    assert res.text == "RTO क्यों बढ़ा?"
    deepseek.generate_json.assert_not_called()


@pytest.mark.anyio
async def test_rejects_a_reply_without_hindi_text():
    service, _ = _service({"text": "Why did RTO go up?"})
    with pytest.raises(AIResponseError):
        await service.translate_prompt(TranslatePromptRequest(text="Why did RTO go up?"))


@pytest.mark.anyio
async def test_ai_errors_are_not_hidden_behind_a_fake_translation():
    service, _ = _service(error=AIServiceUnavailableError("down"))
    with pytest.raises(AIServiceUnavailableError):
        await service.translate_prompt(TranslatePromptRequest(text="Why did RTO go up?"))


def test_endpoint_returns_hindi_text():
    service, _ = _service({"text": "RTO क्यों बढ़ा?"})
    app.dependency_overrides[get_prompt_translator_service] = lambda: service
    try:
        res = TestClient(app).post(
            "/api/reports/translate-prompt", json={"text": "Why did RTO go up?", "target": "hi"}
        )
    finally:
        app.dependency_overrides.pop(get_prompt_translator_service, None)
    assert res.status_code == 200
    assert res.json() == {"text": "RTO क्यों बढ़ा?", "source_language": "english"}


def test_endpoint_reports_ai_outage_as_503():
    service, _ = _service(error=AIServiceUnavailableError("down"))
    app.dependency_overrides[get_prompt_translator_service] = lambda: service
    try:
        res = TestClient(app).post("/api/reports/translate-prompt", json={"text": "Why?"})
    finally:
        app.dependency_overrides.pop(get_prompt_translator_service, None)
    assert res.status_code == 503


@pytest.mark.parametrize(
    "body",
    [{"text": ""}, {"text": "x" * 2001}, {"text": "Why?", "target": "ta"}, {"text": "Why?", "extra": 1}],
)
def test_endpoint_validates_the_request(body):
    res = TestClient(app).post("/api/reports/translate-prompt", json=body)
    assert res.status_code == 422
