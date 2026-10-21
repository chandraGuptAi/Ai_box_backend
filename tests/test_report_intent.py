"""Intent-specific output, scoped evidence, token budgets and export regressions."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from io import BytesIO
import json
from types import SimpleNamespace

import pytest
from docx import Document

from app.config import settings
from app.models.report_schema import ReportResponse
from app.prompts.base import build_messages
from app.prompts.registry import get_report_definition
from app.prompts.report_intent import detect_intent, scope_analysis, compact_context
from app.services.deepseek_service import DeepSeekService
from tests.test_reports import (client, fake_ai, isolated_validation_audit, SAMPLE_REQUEST,
                                FINANCIAL_REQUEST, STRUCTURED_AI, _financial_ai)

DASHBOARD_PROMPT = "Create a shipment insight report in dashboard format for delivered shipments. Keep it short and add graphs."


def concise_response(text="Review the supplied shipment outcomes."):
    return {"title": "Shipment Insights", "executive_summary": {"paragraphs": [text]},
            "findings": [{"title": "Review", "detail": "Review the supplied shipment outcomes.", "severity": "info"}]}


def generate(client, prompt, data=None):
    response = client.post("/api/reports/generate", json={**SAMPLE_REQUEST, "prompt": prompt, **({"data": data} if data else {})})
    assert response.status_code == 200, response.text
    ReportResponse.model_validate(response.json())
    return response


def test_delivered_dashboard_filters_all_evidence_and_caps_output(client, fake_ai):
    fake_ai.response = concise_response("3 delivered orders were reviewed.")
    fake_ai.response["findings"] *= 6
    response = generate(client, DASHBOARD_PROMPT)
    report = response.json()
    structured = report["structured_report"]
    assert report["records_analyzed"] == 3
    assert structured["scope"]["source_records"] == 12
    assert structured["scope"]["denominator"] == "matching_supplied_records"
    assert len(structured["metrics"]) == 2  # internal issue card is excluded
    assert len(structured["charts"]) == 1
    assert sum(structured["charts"][0]["datasets"][0]["data"]) == 3
    assert len(structured["findings"]) == 1  # repeated findings removed
    assert structured["sections"] == structured["recommendations"] == []
    context = fake_ai.messages[1]["content"]
    for key in ("shipping_financial", "top_destination_states", "sample_records", "cancellation_reasons", "delivery_rate_percent"):
        assert key not in context
    assert structured["executive_summary"]["heading"] == "Overview"


def test_zero_delivered_does_not_call_ai_or_invent_performance(client, fake_ai):
    fake_ai.error = AssertionError("No AI call needed for an empty cohort")
    data = {"getOrderList": [{"order_status": "Cancelled", "totalCharge": 99}, {"order_status": "RTO Delivered"}]}
    response = client.post("/api/reports/generate", json={**SAMPLE_REQUEST, "prompt": DASHBOARD_PROMPT, "data": data})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_matching_records"
    assert "X-Report-Access-Token" not in response.headers
    assert not fake_ai.messages


def test_financial_comparison_selects_charge_tables_and_charts(client, fake_ai):
    fake_ai.response = _financial_ai()
    report = generate(client, "Compare shipping charges by courier and payment mode. Include tables, charts and recommendations.", FINANCIAL_REQUEST["data"]).json()
    s = report["structured_report"]
    assert [m["value"] for m in s["metrics"]] == ["10.30", "3.43", "10.00", 5]
    assert {t["title"] for t in report["tables"]} == {"Shipping Charges by Courier", "Shipping Charges by Payment Mode"}
    assert {c["id"] for c in s["charts"]} == {"courier_charges", "payment_charges"}
    assert all(sum(c["datasets"][0]["data"]) == pytest.approx(10.3) for c in s["charts"])
    assert "confirmed losses" in " ".join(s["limitations"])
    assert "sample_records" not in fake_ai.messages[1]["content"]
    assert '"rows"' not in fake_ai.messages[1]["content"]


def test_detailed_report_keeps_relevant_sections_and_recommendations(client, fake_ai):
    fake_ai.response = deepcopy(STRUCTURED_AI)
    fake_ai.response["recommendations"] = [{"action": "Review recorded cancellation reasons.", "rationale": "Identify operational issues.", "priority": "medium"}]
    s = generate(client, "Create a detailed report on status, courier performance and cancellations with charts and tables.").json()["structured_report"]
    assert any(b["type"] == "paragraph" for section in s["sections"] for b in section["blocks"])
    assert not s["recommendations"] and s["charts"]  # issue diagnostics stay internal
    assert all("Shipping Charges" not in section["title"] for section in s["sections"])


@pytest.mark.parametrize("prompt,table_count,chart_count", [
    ("Show only a courier comparison table.", 1, 0),
    ("Show only graphs of shipment status.", 0, 1),
])
def test_visual_only_requests_are_deterministic(client, fake_ai, prompt, table_count, chart_count):
    fake_ai.error = AssertionError("Visual-only report should not call DeepSeek")
    report = generate(client, prompt).json()
    s = report["structured_report"]
    assert len(report["tables"]) == table_count
    assert len(s["charts"]) == chart_count
    assert s["metrics"] == s["findings"] == s["recommendations"] == []
    assert all(b["type"] == "table" for section in s["sections"] for b in section["blocks"])


def test_short_summary_has_no_unrequested_visuals_or_analysis(client, fake_ai):
    fake_ai.response = deepcopy(STRUCTURED_AI)
    s = generate(client, "Give a short shipment summary.").json()["structured_report"]
    assert len(s["executive_summary"]["paragraphs"]) == 1
    assert s["sections"] == s["charts"] == s["recommendations"] == []
    assert len(s["findings"]) <= 3
    assert all("shipping" not in note.lower() for note in s["limitations"])


def test_compact_oversized_prose_does_not_expand_document(client, fake_ai):
    fake_ai.response = concise_response("Review the supplied records. " * 25)
    fake_ai.response["findings"][0]["detail"] *= 20
    s = generate(client, "Give a short shipment summary.").json()["structured_report"]
    assert len(s["executive_summary"]["paragraphs"][0]) <= 350
    assert s["findings"] == []


def test_exports_render_same_chart_and_concise_canonical_content(client, fake_ai):
    fake_ai.response = concise_response("3 delivered orders were reviewed.")
    response = generate(client, DASHBOARD_PROMPT)
    report = response.json()
    headers = {"Authorization": "Bearer " + response.headers["X-Report-Access-Token"]}
    url = "/api/reports/" + report["report_id"]
    fake_ai.error = AssertionError("Exports must not generate again")
    word = client.get(url + "/download?format=docx", headers=headers)
    doc = Document(BytesIO(word.content))
    assert len(doc.inline_shapes) == len(report["structured_report"]["charts"])
    text = " ".join(p.text for p in doc.paragraphs)
    assert "3 delivered orders were reviewed." in text and "Overview" in text
    assert "Executive Summary" not in text and "Analysis focus" not in text
    pdf = client.get(url + "/download?format=pdf", headers=headers)
    assert pdf.status_code == 200 and b"/Subtype /Image" in pdf.content
    import re
    assert len(re.findall(rb"/Type /Page\b", pdf.content)) <= 2
    assert client.get(url + "/download?format=pdf").status_code == 404


def test_compact_context_size_privacy_and_table_metadata():
    definition = get_report_definition("shipment")
    analysis = definition.analyze(SAMPLE_REQUEST["data"])
    intent = detect_intent(DASHBOARD_PROMPT)
    scoped = scope_analysis(analysis, SAMPLE_REQUEST["data"], intent)
    old = build_messages(definition, analysis, DASHBOARD_PROMPT)
    new = build_messages(definition, scoped, DASHBOARD_PROMPT, intent)
    assert sum(len(m["content"]) for m in new) < sum(len(m["content"]) for m in old) * .6
    context = compact_context(scoped, intent)
    assert "sample_records" not in context and "metric_definitions" not in context
    text = json.dumps(context)
    assert "customer" not in text and "invoice" not in text


def _sdk_client(responses, calls):
    async def create(**kwargs):
        calls.append(kwargs)
        return next(responses)
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_json_output_budget_grows_once_on_truncation_and_logs_usage(caplog):
    calls = []
    responses = iter([
        SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=600), choices=[SimpleNamespace(finish_reason="length")]),
        SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=700), choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"title":"valid JSON"}'))]),
    ])
    service = DeepSeekService(replace(settings, deepseek_max_output_tokens=4096,
                                     deepseek_input_cost_per_million=1, deepseek_output_cost_per_million=2))
    service._client = _sdk_client(responses, calls)
    service._get_client = lambda: service._client
    with caplog.at_level("INFO"):
        result = asyncio.run(service.generate_json([{"role": "user", "content": "private payload"}], max_tokens=600))
    assert result == {"title": "valid JSON"}
    assert [call["max_tokens"] for call in calls] == [600, 4096]
    assert all(call["response_format"] == {"type": "json_object"} for call in calls)
    logs = [json.loads(r.message) for r in caplog.records if '"event": "deepseek_usage"' in r.message]
    assert len(logs) == 2
    assert logs[-1]["estimated_cost"] == pytest.approx(.0015)
    assert "private payload" not in caplog.text


def test_exhausted_truncation_is_never_accepted():
    from app.errors import AIResponseError
    responses = iter([SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason="length")])] * 2)
    service = DeepSeekService(replace(settings, deepseek_max_output_tokens=4096))
    calls = []
    service._get_client = lambda: _sdk_client(responses, calls)
    with pytest.raises(AIResponseError) as error:
        asyncio.run(service.generate_json([], max_tokens=600))
    assert error.value.code == "ai_response_truncated"
    assert len(calls) == 2


@pytest.mark.parametrize("kind", ["bar", "pie", "line", "doughnut", "donut"])
def test_existing_chart_types_render_for_exports(kind):
    from app.generators.chart_renderer import chart_png
    from app.models.report_schema import ReportChart, ChartDataset
    chart = ReportChart(id="test", title="Counts", type=kind, labels=["Courier A", "Courier B"],
                        datasets=[ChartDataset(label="Orders", data=[3, 2])])
    assert chart_png(chart).startswith(b"\x89PNG")


@pytest.mark.parametrize("prompt,mode", [
    ("Only graphs, without tables", "graphs_only"),
    ("Only a courier table, no graphs", "table_only"),
    ("Compare delivered and cancelled shipments", "comparison"),
])
def test_explicit_visual_choices_and_comparisons(prompt, mode):
    intent = detect_intent(prompt)
    assert intent.mode == mode
    assert intent.status is None


def test_single_call_always_gets_the_full_output_budget(client, fake_ai, monkeypatch):
    fake_ai.response = concise_response()
    budgets = []
    async def reply(messages, **options):
        budgets.append(options["max_tokens"])
        return fake_ai.response
    monkeypatch.setattr(fake_ai, "generate_json", reply)
    generate(client, "Give a short shipment summary.")
    generate(client, "Give a detailed shipment analysis.")
    # One call per report, so a cut-off answer would be paid for and unusable.
    assert budgets == [settings.deepseek_max_output_tokens, settings.deepseek_max_output_tokens]


def test_delivered_cohort_cannot_supply_a_spurious_courier_percentage(client):
    from app.api.report_routes import get_report_service
    from app.main import app
    from app.services.report_service import ReportService
    from tests.test_reports import SequenceAI
    bad = concise_response("Each courier represents 100% of delivered records.")
    good = concise_response("3 delivered orders were reviewed.")
    ai = SequenceAI([bad, good])
    app.dependency_overrides[get_report_service] = lambda: ReportService(ai)
    response = generate(client, DASHBOARD_PROMPT)
    assert "100%" not in json.dumps(response.json()["structured_report"])
    assert "status_category_breakdown" not in ai.calls[0][1]["content"]


def test_donut_exchange_and_courier_charges_pie_chart(client, fake_ai):
    fake_ai.response = _financial_ai()
    prompt = (
        "create a report for shipment report data analysis based on the below data, the report should be presentive "
        "and in lucid manner, it should consist of graphs and summary, add the donut chart, remove the pie chart and "
        "exchange it with donut chart from Shipment Status Distribution and Status Category show as donut chart "
        "remove the table of Shipping Charges by Courier and show data in pie chart with label data"
    )
    report = generate(client, prompt, FINANCIAL_REQUEST["data"]).json()
    structured = report["structured_report"]
    charts = structured["charts"]
    # Expect 2 charts: courier charges pie chart and status donut chart
    chart_types = {c["id"]: c["type"] for c in charts}
    assert chart_types.get("courier_charges_pie") == "pie"
    assert chart_types.get("shipment_status") == "doughnut"
    # Courier table omitted per prompt
    table_titles = [t["title"] for t in report["tables"]]
    assert "Shipping Charges by Courier" not in table_titles
    assert "Shipping Charges by Payment Mode" in table_titles


def test_courier_freight_single_donut_chart(client, fake_ai):
    fake_ai.response = _financial_ai()
    prompt = (
        "Analyze courier shipping charges and generate a concise financial shipment report. "
        "Present courier freight distribution in a single animated donut chart with pointer labels "
        "and center total, taking the single chart area."
    )
    report = generate(client, prompt, FINANCIAL_REQUEST["data"]).json()
    structured = report["structured_report"]
    charts = structured["charts"]
    assert len(charts) == 1
    assert charts[0]["id"] == "courier_charges_pie"
    assert charts[0]["type"] == "doughnut"
    assert charts[0]["title"] == "Shipping Charges by Courier"


def test_courier_freight_all_available_tables_with_single_donut(client, fake_ai):
    fake_ai.response = _financial_ai()
    prompt = (
        "Analyze courier shipping charges and generate a concise financial shipment report. "
        "Present courier freight distribution in a animated donut chart with pointer labels "
        "and center total, taking the single chart area add multiple table which show the data "
        "in datatable and make it full report it show all required aur avaible table of the data"
    )
    intent = detect_intent(prompt)
    assert intent.all_tables is True
    assert intent.single_chart is True
    assert intent.donut is True
    assert intent.mode == "detailed"

    report = generate(client, prompt, FINANCIAL_REQUEST["data"]).json()
    structured = report["structured_report"]
    # Single chart
    assert len(structured["charts"]) == 1
    assert structured["charts"][0]["id"] == "courier_charges_pie"
    assert structured["charts"][0]["type"] == "doughnut"

    # All available tables are included
    table_titles = {t["title"] for t in report["tables"]}
    assert "Shipping Charges by Courier" in table_titles
    assert "Shipping Charges by Payment Mode" in table_titles
    assert "Shipment Status Summary" in table_titles
    assert "Courier Performance" in table_titles
    assert len(table_titles) >= 4


def test_omit_limitations_and_both_circular_charts(client, fake_ai):
    fake_ai.response = deepcopy(STRUCTURED_AI)
    prompt = (
        "mujhe ekk report bana k dedo jisme undelivery ka data total pehle circle wali graph me dedo "
        "and niche sare jaruri data table me dekha do donut pie chart laga saket ho dono graph me "
        "niche me data scopr and limitation wala portion hata do"
    )
    intent = detect_intent(prompt)
    assert intent.omit_limitations is True
    assert intent.both_circular is True
    assert intent.all_tables is True
    assert intent.donut is True

    report = generate(client, prompt).json()
    structured = report["structured_report"]
    assert all("completeness could not be verified" in n for n in structured["limitations"])
    assert report["content"]["data_limitations"] == []
    assert len(structured["charts"]) == 2
    assert structured["charts"][0]["type"] == "doughnut"
    assert structured["charts"][1]["type"] == "pie"
    assert len(report["tables"]) >= 4


def test_one_bar_one_pie_chart(client, fake_ai):
    fake_ai.response = deepcopy(STRUCTURED_AI)
    prompt = (
        "Analyse my shipment data and generate a detailed performance report. "
        "Please include exactly two charts: "
        "1. A Bar Chart comparing courier-wise total shipment volume and delivery counts. "
        "2. A Pie/Donut Chart showing the overall delivery status breakdown (Delivered, In Transit, Undelivered, and RTO percentages). "
        "Below the charts, include essential data tables showing courier performance metrics, city-wise delivery breakdown, and failure reasons. "
        "Focus on actionable operational insights without adding any data limitations section."
    )
    intent = detect_intent(prompt)
    assert intent.wants_bar is True
    assert intent.both_circular is False
    assert intent.omit_limitations is True

    report = generate(client, prompt).json()
    structured = report["structured_report"]
    assert len(structured["charts"]) == 2
    types = {c["type"] for c in structured["charts"]}
    assert "bar" in types
    assert any(t in {"doughnut", "donut", "pie"} for t in types)


def test_area_and_line_charts(client, fake_ai):
    fake_ai.response = deepcopy(STRUCTURED_AI)
    prompt = (
        "Analyse my shipment data and generate a detailed performance report. "
        "Please include exactly two charts: "
        "1. A area Chart comparing courier-wise total shipment volume and delivery counts. "
        "2. A  line Chart showing the overall delivery status breakdown (Delivered, In Transit, Undelivered, and RTO percentages). "
        "Below the charts, include essential data tables showing courier performance metrics, city-wise delivery breakdown, and failure reasons. "
        "Focus on actionable operational insights without adding any data limitations section."
    )
    report = generate(client, prompt).json()
    structured = report["structured_report"]
    assert len(structured["charts"]) == 2
    assert structured["charts"][0]["type"] == "area"
    assert structured["charts"][0]["id"] == "courier_orders"
    assert structured["charts"][1]["type"] == "line"
    assert structured["charts"][1]["id"] == "shipment_status"


def test_area_chart_without_numbered_spec(client, fake_ai):
    fake_ai.response = concise_response()
    prompt = "Create a shipment report with area chart for couriers"
    report = generate(client, prompt).json()
    structured = report["structured_report"]
    assert len(structured["charts"]) >= 1
    assert any(c["type"] == "area" for c in structured["charts"])







def test_full_budget_truncation_and_invalid_json_cost_exactly_one_call():
    from app.errors import AIResponseError
    for choice in (SimpleNamespace(finish_reason="length"),
                   SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="not json"))):
        responses = iter([SimpleNamespace(usage=None, choices=[choice])] * 3)
        service = DeepSeekService(replace(settings, deepseek_max_output_tokens=4096))
        calls = []
        service._get_client = lambda: _sdk_client(responses, calls)
        with pytest.raises(AIResponseError) as error:
            asyncio.run(service.generate_json([], max_tokens=4096))  # the report flow always sends the full budget
        assert len(calls) == 1
        assert error.value.code in ("ai_response_truncated", "ai_invalid_json")
        assert error.value.details["ai_calls"] == 1


def test_sdk_does_not_retry_billed_calls_by_default():
    assert settings.deepseek_max_retries == 0


def test_deepseek_deadline_covers_the_whole_call(monkeypatch):
    """Keep-alive blank lines reset httpx's read timeout, so the service enforces a total deadline."""
    from app.errors import AIServiceTimeoutError

    class SlowCompletions:
        async def create(self, **kwargs):
            await asyncio.sleep(5)

    service = DeepSeekService(replace(settings, deepseek_timeout_seconds=0.05))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SlowCompletions()))
    monkeypatch.setattr(service, "_get_client", lambda: client)
    with pytest.raises(AIServiceTimeoutError):
        asyncio.run(service.generate_json([{"role": "user", "content": "x"}]))
