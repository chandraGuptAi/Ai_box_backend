"""Report memory from the backend side: lookup before planning, reuse, examples, outcome recording."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from app.config import settings
from app.models.report_schema import ReportRequest
from app.services import report_memory
from app.services.report_service import ReportService
from tests.test_ordered_reports import PlannedAI, section, shipments


class FakeMemory:
    """Ai_Box_Service /api/report_memory: exact reuse of cacheable plans, fixed examples."""

    def __init__(self, examples=(), status=200):
        self.plans, self.examples, self.status = {}, list(examples), status
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request):
        body = json.loads(request.content)
        path = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((path, body))
        if self.status != 200:
            return httpx.Response(self.status)
        if path == "lookup":
            return httpx.Response(200, json={"cached_plan": self.plans.get((body["prompt"].lower(), body["scope_key"])),
                                             "examples": self.examples})
        if path == "record":
            if body["outcome"] == "generated" and body["cacheable"]:
                self.plans[(body["prompt"].lower(), body["scope_key"])] = body["plan"]
            return httpx.Response(200, json={"id": "x"})
        return httpx.Response(200, json={"applied": True})


@pytest.fixture
def memory(monkeypatch):
    fake = FakeMemory()
    monkeypatch.setenv("REPORT_MEMORY", "true")
    monkeypatch.setenv("AI_BOX_SERVICE_URL", "http://ai-box.test")
    real_async, real_sync = httpx.AsyncClient, httpx.Client
    monkeypatch.setattr(report_memory.httpx, "AsyncClient",
                        lambda **kw: real_async(transport=httpx.MockTransport(fake), **kw))
    monkeypatch.setattr(report_memory.httpx, "Client", lambda **kw: real_sync(transport=httpx.MockTransport(fake), **kw))
    return fake


def generate(ai, prompt="Courier wise shipments, call me on 9876543210"):
    request = ReportRequest.model_validate({"report_type": "shipment", "prompt": prompt,
                                            "data": {"orders": shipments()}, "expected_records": 8})
    service = ReportService(ai, config=replace(settings, report_ai_planner=True))
    return asyncio.run(service.generate_report(request))


def planner_calls(ai):
    return [m for m in ai.calls if "[report_plan]" in m[0]["content"]]


def test_second_identical_question_reuses_the_plan_without_an_ai_planning_call(memory):
    first_ai = PlannedAI([section("by_courier", ("courier",))])
    first = generate(first_ai)
    assert len(planner_calls(first_ai)) == 1
    lookup, record = memory.calls[0][1], memory.calls[-1][1]
    assert "9876543210" not in lookup["prompt"] and "[REDACTED]" in lookup["prompt"]  # stored filtered
    assert record["outcome"] == "generated" and record["cacheable"] and record["report_id"] == first.report_id
    assert "source_scope" not in record["plan"]

    second_ai = PlannedAI([section("something_else", ("payment_mode",))])
    second = generate(second_ai)
    assert planner_calls(second_ai) == []                      # plan came from memory
    assert [t.title for t in second.tables] == [t.title for t in first.tables]
    assert second.records_analyzed == 8


def test_similar_questions_are_given_to_the_planner_as_examples(memory):
    memory.examples = [{"prompt": "lane wise shipments", "plan": {"sections": [{"dimensions": ["lane"]}]},
                        "similarity": 0.8, "score": 4}]
    ai = PlannedAI([section("by_courier", ("courier",))])
    generate(ai, "courier wise shipments")
    context = json.loads(planner_calls(ai)[0][-1]["content"])
    assert context["similar_successful_examples"] == [{"request": "lane wise shipments",
                                                       "plan": {"sections": [{"dimensions": ["lane"]}]}}]


def test_memory_down_never_blocks_a_report(memory):
    memory.status = 503
    ai = PlannedAI([section("by_courier", ("courier",))])
    report = generate(ai, "courier wise shipments")
    assert report.records_analyzed == 8 and len(planner_calls(ai)) == 1


def test_plans_with_date_filters_are_not_reused(memory):
    dated = section("early_october", ("courier",),
                    filters=[{"field": "manifest_datetime", "op": "between", "value": ["2026-10-01", "2026-10-15"]}])
    generate(PlannedAI([dated]), "courier wise shipments from 1 to 15 october")
    assert memory.calls[-1][0] == "record" and memory.calls[-1][1]["cacheable"] is False


def test_feedback_is_sent_in_the_background(memory):
    report_memory.send_feedback("report-1", "up")
    assert memory.calls[-1] == ("feedback", {"report_id": "report-1", "signal": "up"})


def test_star_rating_endpoint_checks_the_owner_and_sends_the_rating(memory):
    from fastapi.testclient import TestClient
    from app.api.report_routes import get_report_service
    from app.main import app
    service = ReportService(PlannedAI([section("by_courier", ("courier",))]), config=replace(settings, report_ai_planner=True))
    report = asyncio.run(service.generate_report(ReportRequest.model_validate(
        {"report_type": "shipment", "prompt": "courier wise shipments", "data": {"orders": shipments()},
         "expected_records": 8})))
    token = service.store_report(report)
    app.dependency_overrides[get_report_service] = lambda: service
    try:
        with TestClient(app) as client:
            url, user = f"/api/reports/{report.report_id}/feedback", {"X-User-Id": "102795"}
            ok = client.post(url, json={"rating": 4}, headers={**user, "Authorization": f"Bearer {token}"})
            assert ok.status_code == 202, ok.text
            assert memory.calls[-1] == ("feedback", {"report_id": report.report_id, "signal": "rating:4"})
            assert client.post(url, json={"rating": 6}, headers={**user, "Authorization": f"Bearer {token}"}).status_code == 422
            assert client.post(url, json={"rating": 4, "signal": "up"},
                               headers={**user, "Authorization": f"Bearer {token}"}).status_code == 422
            assert client.post(url, json={"rating": 4}, headers={**user, "Authorization": "Bearer wrong"}).status_code == 404
            assert client.post(url, json={"rating": 4}, headers={"Authorization": f"Bearer {token}"}).status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_memory_is_off_without_the_service(monkeypatch):
    monkeypatch.setenv("AI_BOX_SERVICE_URL", "")
    monkeypatch.setenv("REPORT_MEMORY", "true")
    ai = PlannedAI([section("by_courier", ("courier",))])
    assert generate(ai, "courier wise shipments").records_analyzed == 8
