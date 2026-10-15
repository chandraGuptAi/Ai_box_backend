import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.skills.prompt_skill import prompt_skill, PromptSkill
from app.errors import InvalidPromptError
from app.services.report_service import ReportService
from app.models.report_schema import ReportRequest


client = TestClient(app)


def test_valid_analytical_prompts():
    skill = PromptSkill()
    valid_prompts = [
        "Analyse my shipment data and generate a detailed performance report.",
        "Please include exactly two charts: 1. A Bar Chart comparing courier volume, 2. A Pie Chart of delivery status.",
        "Compare courier performance and failure reasons.",
        "Show RTO percentage and top cities with highest delivery delays.",
        "Give me a breakdown of delivered vs in transit orders.",
        "COD vs Prepaid volume comparison",
        "Summarize remittance payouts and deduction charges.",
    ]
    for p in valid_prompts:
        res = skill.validate(p, report_type="shipment")
        assert res.is_valid is True, f"Failed for valid prompt: {p} (reason: {res.reason})"
        assert res.category == "valid"


def test_hinglish_analytical_prompts():
    skill = PromptSkill()
    hinglish_prompts = [
        "courier performance check karo",
        "ek bar chart aur ek pie chart banao",
        "mujhe delivery report chahiye",
        "sabse jyada rto kaha hua hai batao",
        "city wise breakdown dikhao",
    ]
    for p in hinglish_prompts:
        res = skill.validate(p, report_type="shipment")
        assert res.is_valid is True, f"Failed for Hinglish prompt: {p} (reason: {res.reason})"


def test_gibberish_prompts():
    skill = PromptSkill()
    gibberish = [
        "asdfghjkl",
        "zzzzzzzzzz",
        "qwertyuiopasdfghjkl",
        "????!!!!!......",
        "1234567890123",
        "bcdfghjklmnp",
    ]
    for p in gibberish:
        res = skill.validate(p, report_type="shipment")
        assert res.is_valid is False, f"Expected gibberish to fail: {p}"
        assert res.category == "gibberish"
        assert len(res.suggested_prompts) > 0


def test_greeting_only_prompts():
    skill = PromptSkill()
    greetings = ["hi", "hello", "hey there", "good morning", "test", "ok", "cool"]
    for p in greetings:
        res = skill.validate(p, report_type="shipment")
        assert res.is_valid is False, f"Expected greeting to fail: {p}"
        assert res.category == "greeting_only"
        assert "greeting" in res.message.lower()


def test_off_topic_prompts():
    skill = PromptSkill()
    off_topics = [
        "recipe for chocolate cake",
        "who won the cricket match yesterday",
        "write a poem about the ocean",
        "give me python code for binary search",
        "tell me a funny joke",
    ]
    for p in off_topics:
        res = skill.validate(p, report_type="shipment")
        assert res.is_valid is False, f"Expected off-topic to fail: {p}"
        assert res.category == "off_topic"
        assert "specializes in logistics" in res.message


def test_vague_prompts():
    skill = PromptSkill()
    vague = ["data", "something", "do it", "help"]
    for p in vague:
        res = skill.validate(p, report_type="shipment")
        assert res.is_valid is False, f"Expected vague to fail: {p}"
        assert res.category == "too_vague"


def test_prompt_skill_blueprint_and_suggestions():
    skill = PromptSkill()
    bp = skill.get_skill_blueprint()
    assert bp["name"] == "prompt-validation-skill"
    assert "capabilities" in bp

    sug = skill.get_suggestions("shipment")
    assert len(sug) >= 3
    assert any("courier" in s.lower() for s in sug)

    remit_sug = skill.get_suggestions("paid_remittance")
    assert any("remittance" in s.lower() for s in remit_sug)


def test_api_prompt_validate_endpoint():
    # Test valid
    resp = client.post(
        "/api/skills/prompt/validate",
        json={"prompt": "Analyse courier performance", "report_type": "shipment"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_valid"] is True
    assert data["category"] == "valid"

    # Test invalid gibberish
    resp = client.post(
        "/api/skills/prompt/validate",
        json={"prompt": "asdfghjkl", "report_type": "shipment"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_valid"] is False
    assert data["category"] == "gibberish"
    assert "could not be understood" in data["message"]


def test_api_prompt_suggestions_and_blueprint():
    resp = client.get("/api/skills/prompt/blueprint")
    assert resp.status_code == 200
    assert resp.json()["name"] == "prompt-validation-skill"

    resp = client.get("/api/skills/prompt/suggestions?report_type=shipment")
    assert resp.status_code == 200
    assert "suggestions" in resp.json()
    assert len(resp.json()["suggestions"]) > 0


import asyncio


def test_report_service_rejects_gibberish():
    async def _run():
        service = ReportService(None)
        req = ReportRequest(
            report_type="shipment",
            prompt="asdfghjkl",
            data={"getOrderList": [{"awb": "1", "current_status": "Delivered"}]},
        )
        with pytest.raises(InvalidPromptError) as exc_info:
            await service.generate_report(req)
        assert exc_info.value.code == "invalid_prompt"
        assert exc_info.value.category == "validation"
        assert "could not be understood" in exc_info.value.message

    asyncio.run(_run())


def test_report_service_rejects_off_topic():
    async def _run():
        service = ReportService(None)
        req = ReportRequest(
            report_type="shipment",
            prompt="how to cook biryani at home",
            data={"getOrderList": [{"awb": "1", "current_status": "Delivered"}]},
        )
        with pytest.raises(InvalidPromptError) as exc_info:
            await service.generate_report(req)
        assert exc_info.value.code == "invalid_prompt"
        assert exc_info.value.category == "validation"
        assert "specializes in logistics" in exc_info.value.message

    asyncio.run(_run())

