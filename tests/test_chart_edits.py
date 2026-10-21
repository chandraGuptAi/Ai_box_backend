"""Regression: a follow-up that removes or re-types one chart changes exactly that chart.

Bug: "Remove only the pie chart and keep the other graph unchanged" could delete every chart, because
follow-up chart removal was decided by prompt regexes with a "remove everything" fallback, and the
PDF re-typed charts from the follow-up wording. Edits are now resolved against the report's own
charts (app/charts/edit.py) and applied as a patch; exports draw the stored charts.
"""

import copy
from io import BytesIO

import pytest
from pypdf import PdfReader

from tests.test_reports import client, fake_ai  # noqa: F401  (fixtures)

USER = {"X-User-Id": "chart-edit-tests"}
DATA = {"getOrderList": [
    {"order_id": "1", "order_status": "Delivered", "courier_name": "Delhivery", "order_date": "2025-01-01"},
    {"order_id": "2", "order_status": "In Transit", "courier_name": "BlueDart", "order_date": "2025-01-02"},
    {"order_id": "3", "order_status": "RTO", "courier_name": "Delhivery", "order_date": "2025-01-03"},
    {"order_id": "4", "order_status": "Delivered", "courier_name": "Xpressbees", "order_date": "2025-01-04"},
    {"order_id": "5", "order_status": "Delivered", "courier_name": "BlueDart", "order_date": "2025-01-05"},
]}
NARRATIVE = {"title": "Shipment Overview", "executive_summary": {"heading": "Overview", "paragraphs": ["Initial analysis."]},
             "findings": [], "recommendations": [], "sections": []}
TWO = "Show shipment status as a pie chart and courier-wise shipments as a bar chart"
THREE = "Show a pie chart of shipment status, a bar chart of courier volume and a line chart of daily orders"


def generate(client, prompt, previous=None):
    body = {"report_type": "shipment", "prompt": prompt, "data": DATA}
    if previous is not None:
        body["previous_report"] = previous
    return client.post("/api/reports/generate", json=body)


@pytest.fixture
def two_charts(client, fake_ai):
    fake_ai.response = NARRATIVE
    report = generate(client, TWO).json()
    charts = report["structured_report"]["charts"]
    assert [c["type"] for c in charts] == ["pie", "bar"]
    assert len({c["id"] for c in charts}) == 2
    return report


def charts_of(report):
    return report["structured_report"]["charts"]


def edit(client, report, prompt):
    response = generate(client, prompt, previous=copy.deepcopy(report))
    assert response.status_code == 200, response.text
    return response.json()


# --- 1-3: removal keeps every other chart exactly as it was -----------------------------------

@pytest.mark.parametrize("prompt", [
    "Remove only the pie chart and keep the other graph unchanged.",
    "Remove the shipment status pie chart.",
    "Remove the chart showing shipment status",   # used to delete both charts
    "Remove chart 1",                             # used to delete both charts
    "remove the first chart",
    "Delete the status pie chart but keep the bar chart",
    "Get rid of the pie chart",
    "Pie chart remove kar do, baaki graph same rakho",
])
def test_removing_the_first_chart_leaves_the_second_identical(client, fake_ai, two_charts, prompt):
    pie, bar = charts_of(two_charts)
    calls = fake_ai.calls
    result = edit(client, two_charts, prompt)
    assert charts_of(result) == [bar]                 # exactly one remains, deeply equal
    assert fake_ai.calls == calls                     # patched, not regenerated
    assert result["report_id"] == two_charts["report_id"]
    assert result["generation"]["chart_edit"] == {"removed": [pie["id"]], "retyped": {}}


@pytest.mark.parametrize("prompt", ["Remove the bar chart", "remove the second chart", "Remove the courier chart and keep the pie"])
def test_removing_the_second_chart_leaves_the_first_identical(client, two_charts, prompt):
    pie, _ = charts_of(two_charts)
    assert charts_of(edit(client, two_charts, prompt)) == [pie]


def test_removing_the_middle_of_three_charts(client, fake_ai):
    fake_ai.response = NARRATIVE
    report = generate(client, THREE).json()
    first, middle, last = charts_of(report)
    assert middle["type"] == "bar"
    assert charts_of(edit(client, report, "Remove the bar chart and leave the other two")) == [first, last]
    assert charts_of(edit(client, report, "remove chart 2")) == [first, last]


def test_doughnut_is_removed_when_the_user_calls_it_a_pie(client, fake_ai):
    fake_ai.response = NARRATIVE
    report = generate(client, "Create a donut chart of delivery status and a bar chart of courier volume").json()
    doughnut, bar = charts_of(report)
    assert doughnut["type"] == "doughnut"
    assert charts_of(edit(client, report, "Remove only the pie chart and keep the other graph unchanged.")) == [bar]


def test_all_charts_are_removed_only_when_asked_for(client, two_charts):
    assert charts_of(edit(client, two_charts, "remove all charts")) == []
    assert charts_of(edit(client, two_charts, "remove both graphs")) == []


# --- 4: re-typing changes only the type of that one chart ------------------------------------

def test_changing_the_pie_to_a_bar_keeps_id_values_and_the_other_chart(client, two_charts):
    pie, bar = charts_of(two_charts)
    result = edit(client, two_charts, "Change only the shipment status pie chart to a bar chart.")
    converted, other = charts_of(result)
    assert converted == {**pie, "type": "bar"}        # same ID, labels and verified values
    assert other == bar
    assert len(charts_of(result)) == 2                # no duplicate chart added


def test_conversion_that_would_misrepresent_data_is_refused(client, two_charts):
    response = generate(client, "change the pie chart to a heatmap", previous=two_charts)
    assert response.status_code == 422
    assert "Nothing was changed" in response.json()["error"]["message"]


# --- 5-6: missing or ambiguous targets change nothing -----------------------------------------

def test_removing_a_chart_that_does_not_exist_is_an_error(client, two_charts):
    response = generate(client, "remove the line chart", previous=two_charts)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "chart_not_found"
    assert "Nothing was changed" in error["message"]
    assert [c["id"] for c in error["details"]["charts"]] == [c["id"] for c in charts_of(two_charts)]


@pytest.mark.parametrize("prompt", ["remove the chart", "chart hata do", "Include delivery status breakdown remove the chart"])
def test_ambiguous_target_asks_for_clarification(client, two_charts, prompt):
    response = generate(client, prompt, previous=two_charts)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "chart_target_ambiguous"
    assert len(error["details"]["charts"]) == 2


# --- 7: an edited report saves and reloads as edited ------------------------------------------

def test_saved_edited_report_reloads_with_the_edit(client, two_charts):
    _, bar = charts_of(two_charts)
    edited = edit(client, two_charts, "Remove only the pie chart and keep the other graph unchanged.")
    saved = client.post("/api/reports/save", headers=USER,
                        json={"report_id": edited["report_id"], "name": "Edited charts", "report": edited})
    assert saved.status_code == 200, saved.text
    try:
        reloaded = client.get(f"/api/reports/saved/{edited['report_id']}", headers=USER).json()
        assert charts_of(reloaded["report"]) == [bar]
    finally:
        client.delete(f"/api/reports/saved/{edited['report_id']}", headers=USER)


# --- 8-9: exports draw exactly the report's charts, in their stored types ---------------------

@pytest.fixture
def drawn(monkeypatch):
    """(id, type) of every chart the PDF/DOCX exporters draw."""
    from app.charts import render
    from app.generators import docx_generator, pdf_generator
    calls = []

    def record(chart):
        calls.append((chart.id, chart.type))
        return original(chart)

    original = render.render_png
    monkeypatch.setattr(pdf_generator, "render_png", record)
    monkeypatch.setattr(docx_generator, "render_png", record)
    return calls


def test_pdf_of_edited_report_shows_exactly_the_remaining_chart(client, two_charts, drawn):
    pie, bar = charts_of(two_charts)
    edited = edit(client, two_charts, "Remove only the pie chart and keep the other graph unchanged.")
    pdf = client.post("/api/reports/export/pdf", json=edited)
    assert pdf.status_code == 200
    # The bar chart stays a bar: the "pie" in the edit instruction no longer re-types it.
    assert drawn == [(bar["id"], "bar")]
    text = "".join(page.extract_text() for page in PdfReader(BytesIO(pdf.content)).pages)
    assert bar["title"] in text and pie["title"] not in text

    drawn.clear()
    assert client.post("/api/reports/export/docx", json=edited).status_code == 200
    assert drawn == [(bar["id"], "bar")]


def test_pdf_of_retyped_report_draws_the_new_type(client, two_charts, drawn):
    pie, bar = charts_of(two_charts)
    edited = edit(client, two_charts, "Change only the shipment status pie chart to a bar chart.")
    assert client.post("/api/reports/export/pdf", json=edited).status_code == 200
    assert drawn == [(pie["id"], "bar"), (bar["id"], "bar")]


@pytest.mark.parametrize("prompt", ["Generate shipment analysis in graphical form", "Show my shipments visually"])
def test_graphical_request_produces_charts_and_the_pdf_draws_them(client, fake_ai, drawn, prompt):
    fake_ai.response = NARRATIVE
    report = generate(client, prompt).json()
    charts = charts_of(report)
    assert charts, "a graphical request must produce charts when the data supports them"
    assert client.post("/api/reports/export/pdf", json=report).status_code == 200
    assert drawn == [(c["id"], c["type"]) for c in charts]


# --- 10: chart edits never touch KPIs, tables or text -----------------------------------------

def test_chart_edit_does_not_change_kpis_tables_or_text(client, two_charts):
    edited = edit(client, two_charts, "Change only the shipment status pie chart to a bar chart.")
    edited = edit(client, edited, "remove the second chart")
    for key in ("metrics", "key_metrics", "tables", "content", "records_analyzed"):
        assert edited[key] == two_charts[key], key
    before, after = two_charts["structured_report"], edited["structured_report"]
    for key in ("metrics", "sections", "findings", "recommendations", "executive_summary", "limitations"):
        assert after[key] == before[key], key


def test_mixed_follow_up_adds_content_and_removes_only_the_named_chart(client, fake_ai, two_charts):
    _, bar = charts_of(two_charts)
    fake_ai.response = {**NARRATIVE, "sections": [{"id": "couriers", "title": "Courier Notes", "order": 1,
                                                  "blocks": [{"type": "paragraph", "text": "Courier detail."}]}]}
    result = edit(client, two_charts, "Add a courier table and remove the pie chart")
    assert [c["id"] for c in charts_of(result)][0] == bar["id"]
    assert all(c["id"] != charts_of(two_charts)[0]["id"] for c in charts_of(result))
