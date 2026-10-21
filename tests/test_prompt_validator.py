"""Unit tests for the AI Prompt Validator and Smart Suggestions endpoint (/api/reports/validate-prompt)."""

import pytest
from unittest.mock import AsyncMock, patch
from starlette.testclient import TestClient

from app.main import app
from app.models.report_schema import ValidatePromptRequest, ValidatePromptResponse
from app.services.prompt_validator_service import (
    PromptValidatorService,
    detect_language_heuristic,
    detect_intent_heuristic,
)


@pytest.fixture
def client():
    return TestClient(app)


def test_empty_prompt_validation(client):
    res = client.post("/api/reports/validate-prompt", json={"prompt": "   ", "report_type": "shipment"})
    assert res.status_code == 422 or (res.status_code == 200 and not res.json()["is_valid"])


def test_heuristic_language_detection():
    assert detect_language_heuristic("show shipment report") == "english"
    assert detect_language_heuristic("मुझे डिलीवरी की रिपोर्ट चाहिए") == "hindi"
    assert detect_language_heuristic("Mujhe shipment ka graph chahiye") == "hinglish"


def test_heuristic_intent_detection():
    assert detect_intent_heuristic("show shipment graph") == "graphical_report"
    assert detect_intent_heuristic("Remove one chart and keep the other") == "remove_chart"
    assert detect_intent_heuristic("Change pie chart to bar chart") == "change_chart_type"
    assert detect_intent_heuristic("Add courier performance chart") == "add_chart"
    assert detect_intent_heuristic("Summarize the report in two sentences") == "summarize_report"
    assert detect_intent_heuristic("Generate complete shipment analysis") == "create_report"


@pytest.mark.anyio
async def test_deepseek_fallback_on_failure():
    mock_deepseek = AsyncMock()
    mock_deepseek.generate_json.side_effect = RuntimeError("DeepSeek connection timed out")
    service = PromptValidatorService(deepseek=mock_deepseek)

    req = ValidatePromptRequest(
        prompt="Mujhe shipment ka graph chahiye",
        report_type="shipment_analysis",
    )
    res = await service.validate_prompt(req)
    assert res.is_valid is True
    assert res.detected_language == "hinglish"
    assert res.detected_intent == "graphical_report"
    assert res.needs_improvement is True
    assert res.suggested_prompt is not None


@pytest.mark.anyio
async def test_clear_prompt_returns_no_suggestion():
    mock_deepseek = AsyncMock()
    mock_deepseek.generate_json.return_value = {
        "is_valid": True,
        "needs_improvement": False,
        "detected_language": "english",
        "detected_intent": "create_report",
        "suggested_prompt": None,
        "reason": "Prompt is already precise and comprehensive.",
        "missing_fields": [],
        "requires_clarification": False,
    }
    service = PromptValidatorService(deepseek=mock_deepseek)

    req = ValidatePromptRequest(
        prompt="Generate a detailed shipment delivery report for last 30 days with city-wise courier performance and RTO rate analysis.",
        report_type="shipment_analysis",
    )
    res = await service.validate_prompt(req)
    assert res.is_valid is True
    assert res.needs_improvement is False
    assert res.suggested_prompt is None


@pytest.mark.anyio
async def test_remove_chart_intent_preserved():
    mock_deepseek = AsyncMock()
    mock_deepseek.generate_json.return_value = {
        "is_valid": True,
        "needs_improvement": False,
        "detected_language": "english",
        "detected_intent": "remove_chart",
        "suggested_prompt": None,
        "reason": "User requested chart removal.",
        "missing_fields": [],
        "requires_clarification": False,
    }
    service = PromptValidatorService(deepseek=mock_deepseek)

    req = ValidatePromptRequest(
        prompt="Remove one chart and keep the other.",
        report_type="shipment_analysis",
        current_report_id="rep-123",
    )
    res = await service.validate_prompt(req)
    assert res.detected_intent == "remove_chart"
    assert res.needs_improvement is False


@pytest.mark.anyio
async def test_change_chart_type_intent():
    mock_deepseek = AsyncMock()
    mock_deepseek.generate_json.return_value = {
        "is_valid": True,
        "needs_improvement": True,
        "detected_language": "english",
        "detected_intent": "change_chart_type",
        "suggested_prompt": "Change the courier volume pie chart to a vertical bar chart.",
        "reason": "Clarified which chart to convert.",
        "missing_fields": [],
        "requires_clarification": False,
    }
    service = PromptValidatorService(deepseek=mock_deepseek)

    req = ValidatePromptRequest(
        prompt="Change pie chart to bar chart",
        report_type="shipment_analysis",
    )
    res = await service.validate_prompt(req)
    assert res.detected_intent == "change_chart_type"
    assert res.needs_improvement is True
    assert "bar chart" in res.suggested_prompt.lower()


def test_api_endpoint_via_client(client):
    payload = {
        "prompt": "show shipment graphical report",
        "report_type": "shipment_analysis",
        "date_range": "last_60_days",
    }
    res = client.post("/api/reports/validate-prompt", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert "is_valid" in data
    assert "needs_improvement" in data
    assert "detected_language" in data
    assert "detected_intent" in data
    assert "suggested_prompt" in data
