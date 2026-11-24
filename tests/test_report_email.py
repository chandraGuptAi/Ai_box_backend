"""Report email: a short AI-written email + the report PDF, created as a Gmail/Outlook draft (Ai_Box_Service mocked)."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.report_email_routes import get_email_ai
from app.api.report_routes import get_report_service
from app.main import app
from app.models.report_schema import ReportResponse, ReportTable
from app.services import ai_box_email
from app.services.report_email import (EmailContent, default_language, fallback_content, render_email,
                                       report_period, summary_tables, unverified_numbers)
from app.services.report_service import ReportService
from tests.test_reports import SAMPLE_REQUEST, FakeAI

EMAIL_AI = {
    "subject": "Shipment report: 12 orders",
    "greeting": "Hello team,",
    "body": "Please find the shipment report attached. 12 orders were analysed. See https://evil.example/login "
            "<script>x()</script>",
    "points": ["- 3 (25.0%) delivered", "**4 (33.3%)** cancelled"],
    "closing": "Let me know if you have questions.",
}
CONNECTION = "0b3f6a52-6f1e-4bb2-9a55-3b5f3c2f0a10"
LOGIN = {"X-Shipment-Authorization": "Bearer 123|abcdefghijklmnop"}
USER = {"X-User-Id": "102795"}
DRAFT = {"draft_id": "r-1", "open_url": "https://mail.google.com/mail/?authuser=a%40b.co#drafts?compose=18c",
         "provider": "gmail", "from_email": "a@b.co", "subject": "x", "attachment_name": "report.pdf"}
STATUS_TABLE = ReportTable(title="Shipment Status Breakdown",
                           columns=["Shipment Status", "Customer Name", "Orders", "Shipment Share (%)"],
                           rows=[["Pickup Scheduled", "Ravi Kumar", "29", "61.70"], ["Cancelled", "Asha", "15", "31.90"],
                                 ["In Transit", "Mohan", "3", "6.40"], ["Delivered", "Sita", "0", "0.00"]])


@pytest.fixture
def report_ai():
    return FakeAI()


@pytest.fixture
def email_ai():
    return FakeAI(response=dict(EMAIL_AI))


@pytest.fixture
def client(report_ai, email_ai):
    app.dependency_overrides[get_report_service] = lambda: ReportService(report_ai)
    app.dependency_overrides[get_email_ai] = lambda: email_ai
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def generated(client):
    response = client.post("/api/reports/generate", json=SAMPLE_REQUEST, headers=USER)
    assert response.status_code == 200, response.text
    return response.json(), {"Authorization": "Bearer " + response.headers["X-Report-Access-Token"], **USER, **LOGIN}


@pytest.fixture
def ai_box(monkeypatch):
    """Fake Ai_Box_Service /api/email/internal/draft."""
    calls: list[httpx.Request] = []
    replies: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return replies.pop(0) if replies else httpx.Response(200, json=DRAFT)
    monkeypatch.setenv("AI_BOX_SERVICE_URL", "http://ai-box:8100")
    monkeypatch.setenv("AI_BOX_SERVICE_TOKEN", "box-secret")
    monkeypatch.setenv("AI_BOX_EMAIL_INTERNAL_KEY", "internal-key")
    monkeypatch.setattr(ai_box_email, "TRANSPORT", httpx.MockTransport(handler))
    return calls, replies


def multipart_parts(request: httpx.Request) -> dict[str, bytes]:
    boundary = request.headers["content-type"].split("boundary=")[1].encode()
    parts = {}
    for chunk in request.content.split(b"--" + boundary)[1:-1]:
        head, _, body = chunk.strip(b"\r\n").partition(b"\r\n\r\n")
        parts[head.split(b'name="')[1].split(b'"')[0].decode()] = body
    return parts


def compose(client, report, headers, **body):
    return client.post(f"/api/reports/{report['report_id']}/email/compose", headers=headers,
                       json={"connection_id": CONNECTION} | body)


def sent_metadata(ai_box) -> dict:
    return json.loads(multipart_parts(ai_box[0][-1])["metadata"])


# ---- compose --------------------------------------------------------------------------------------

def test_compose_creates_a_draft_with_a_short_email_and_the_pdf(client, generated, ai_box, email_ai):
    report, headers = generated
    res = compose(client, report, headers, tone="brief", language="hi", instruction="focus on cancellations")
    assert res.status_code == 200, res.text
    assert res.json() == {**DRAFT, "content_source": "ai"}

    request = ai_box[0][0]
    assert str(request.url) == "http://ai-box:8100/api/email/internal/draft"
    assert request.headers["x-email-internal-key"] == "internal-key"
    assert request.headers["x-ai-box-service-token"] == "box-secret"
    assert request.headers["x-shipment-authorization"] == LOGIN["X-Shipment-Authorization"]
    parts = multipart_parts(request)
    metadata = json.loads(parts["metadata"])
    assert metadata["connection_id"] == CONNECTION and metadata["report_id"] == report["report_id"]
    assert metadata["subject"] == "Shipment report: 12 orders"
    html = metadata["html"]
    assert "<li>3 (25.0%) delivered</li>" in html and "<li>4 (33.3%) cancelled</li>" in html
    assert "evil.example" not in html and "<script>" not in html
    assert "<table" not in html and "Key figures" not in html          # an email, not a copy of the report
    assert "धन्यवाद," in html and metadata["text"].endswith("धन्यवाद,")   # language="hi": Hindi sign-off
    assert parts["attachment"].startswith(b"%PDF")

    sent = json.dumps(email_ai.messages, ensure_ascii=False)
    assert "REPORT_FACTS" in sent and "focus on cancellations" in sent and "Hindi" in sent
    for order in SAMPLE_REQUEST["data"]["getOrderList"]:
        assert str(order["customer_name"]) not in sent and str(order["customer_email"]) not in sent


def test_an_invented_number_falls_back_to_the_report_text(client, generated, ai_box, email_ai):
    email_ai.response["body"] = "12 orders were analysed and 999 were lost."
    report, headers = generated
    res = compose(client, report, headers)
    assert res.status_code == 200 and res.json()["content_source"] == "report"
    assert "999" not in sent_metadata(ai_box)["html"]


def test_the_email_is_still_created_when_the_ai_fails(client, generated, ai_box, email_ai):
    email_ai.error = RuntimeError("AI down")
    report, headers = generated
    res = compose(client, report, headers)
    assert res.status_code == 200 and res.json()["content_source"] == "report"
    assert sent_metadata(ai_box)["subject"].startswith(ReportResponse.model_validate(report).structured_report.title)


def test_compose_needs_access_to_the_report(client, generated, ai_box):
    report, _ = generated
    res = compose(client, report, {"Authorization": "Bearer wrong", "X-User-Id": "someone-else", **LOGIN})
    assert res.status_code == 404 and ai_box[0] == []


def test_compose_needs_the_bigship_login(client, generated, ai_box):
    report, headers = generated
    res = compose(client, report, {k: v for k, v in headers.items() if k != "X-Shipment-Authorization"})
    assert res.status_code == 401 and ai_box[0] == []


def test_ai_box_service_errors_pass_through(client, generated, ai_box):
    report, headers = generated
    ai_box[1].append(httpx.Response(409, json={"detail": {
        "code": "EMAIL_RECONNECT_REQUIRED", "message": "Your Gmail access has expired. Connect the mailbox again.",
        "retryable": False, "action": "reconnect", "provider": "gmail"}}))
    res = compose(client, report, headers)
    assert res.status_code == 409
    error = res.json()["error"]
    assert error["code"] == "email_reconnect_required" and "Gmail" in error["message"]
    assert error["details"] == {"action": "reconnect", "provider": "gmail"}


def test_compose_when_not_configured(client, generated, monkeypatch):
    monkeypatch.delenv("AI_BOX_EMAIL_INTERNAL_KEY", raising=False)
    report, headers = generated
    res = compose(client, report, headers)
    assert res.status_code == 503 and res.json()["error"]["code"] == "email_not_configured"


def test_an_unreachable_email_service(client, generated, monkeypatch, ai_box):
    def timeout(request):
        raise httpx.ReadTimeout("no answer", request=request)
    monkeypatch.setattr(ai_box_email, "TRANSPORT", httpx.MockTransport(timeout))
    report, headers = generated
    res = compose(client, report, headers)
    assert res.status_code == 503 and res.json()["error"]["code"] == "email_service_unavailable"


def test_a_report_the_server_forgot_can_be_sent_by_the_page(client, generated, ai_box):
    report, headers = generated
    no_token = {k: v for k, v in headers.items() if k != "Authorization"}
    lost = client.post("/api/reports/unknown-id/email/compose", headers=no_token, json={"connection_id": CONNECTION})
    assert lost.status_code == 404
    res = client.post(f"/api/reports/{report['report_id']}/email/compose", headers={**no_token, "X-User-Id": "other"},
                      json={"connection_id": CONNECTION, "report": report})
    assert res.status_code == 200, res.text
    assert multipart_parts(ai_box[0][-1])["attachment"].startswith(b"%PDF")


def test_a_changed_report_sent_by_the_page_is_refused(client, generated, ai_box, email_ai):
    report, _ = generated
    tampered = json.loads(json.dumps(report))
    tampered["key_metrics"][0]["value"] = "999999"
    res = client.post(f"/api/reports/{report['report_id']}/email/compose",
                      headers={"X-User-Id": "other", **LOGIN}, json={"connection_id": CONNECTION, "report": tampered})
    assert res.status_code >= 400 and ai_box[0] == [] and email_ai.calls == 0


# ---- the email follows the user's report request ----------------------------------------------------

def test_the_ai_gets_the_original_request_and_the_result_table(client, generated, ai_box, email_ai):
    report, headers = generated
    compose(client, report, headers)
    facts = json.loads(email_ai.messages[1]["content"].split("REPORT_FACTS:\n", 1)[1])
    assert facts["original_request"] == SAMPLE_REQUEST["prompt"]
    table = ReportResponse.model_validate(report).tables[0]
    assert facts["tables"][0]["title"] == table.title and facts["tables"][0]["rows"][0] == table.rows[0][:6]
    system = email_ai.messages[0]["content"]
    assert "original_request" in system and "must NOT repeat the report" in system


def test_a_hindi_request_gets_a_hindi_email_unless_chosen(client, generated, ai_box, email_ai, monkeypatch):
    from app.api import report_email_routes
    report, headers = generated
    monkeypatch.setattr(report_email_routes, "default_language", lambda r: "hi")
    compose(client, report, headers)
    assert "Hindi" in email_ai.messages[1]["content"] and "धन्यवाद," in sent_metadata(ai_box)["html"]
    compose(client, report, headers, language="en")
    assert "Hindi" not in email_ai.messages[1]["content"]


def test_default_language_follows_the_request(generated):
    report = ReportResponse.model_validate(generated[0])
    assert default_language(report.model_copy(update={"prompt": "Give me a status breakdown"})) == "en"
    assert default_language(report.model_copy(update={"prompt": "सभी ऑर्डर का स्टेटस ब्रेकडाउन दिखाओ"})) == "hi"


def test_the_period_reads_like_a_person_wrote_it(generated):
    report = ReportResponse.model_validate(generated[0])

    def with_period(start, end):
        scope = {**report.structured_report.scope, "selected_period": {"from_date": start, "to_date": end,
                                                                         "date_field": "manifest_datetime"}}
        structured = report.structured_report.model_copy(update={"scope": scope})
        return report_period(report.model_copy(update={"structured_report": structured}))
    assert with_period("2026-11-17", "2026-11-23") == "17–23 Nov 2026"
    assert with_period("2026-10-28", "2026-11-03") == "28 Oct – 3 Nov 2026"
    assert with_period("2025-12-30", "2026-01-02") == "30 Dec 2025 – 2 Jan 2026"
    assert with_period(None, None) == ""


# ---- building blocks ------------------------------------------------------------------------------

def test_render_is_a_plain_email_with_everything_escaped():
    content = EmailContent(subject="Weekly", body='<img src=x onerror=alert(1)> "ok"',
                           points=["<a href='x'>click</a>"], closing="Thanks")
    html, text = render_email(content)
    assert "<img" not in html and "&lt;img" in html and "<a href" not in html
    assert "<table" not in html and "Hello," in html and "Regards," in html
    assert text.splitlines()[0] == "Hello," and "• <a href='x'>click</a>" in text and text.endswith("Regards,")


def test_the_fallback_email_is_short_and_uses_the_result_table(generated):
    report = ReportResponse.model_validate(generated[0]).model_copy(update={"tables": [STATUS_TABLE]})
    content = fallback_content(report)
    assert content.points == ["Pickup Scheduled: 29 (61.70)", "Cancelled: 15 (31.90)", "In Transit: 3 (6.40)"]
    assert content.body.startswith(f"Please find attached the {report.structured_report.title} report")
    assert "attached PDF" in content.closing


def test_personal_columns_never_reach_the_ai(generated):
    report = ReportResponse.model_validate(generated[0]).model_copy(update={"tables": [STATUS_TABLE]})
    table = summary_tables(report)[0]
    assert table["columns"] == ["Shipment Status", "Orders", "Shipment Share (%)"]
    assert "Ravi Kumar" not in json.dumps(table)


def test_unverified_numbers_ignores_small_counts_and_report_numbers():
    draft = EmailContent(subject="x", body="12 orders, 3 issues, 4,500 rupees and 33.3% cancelled")
    facts = {"key_metrics": [{"label": "Orders", "value": "12"}, {"label": "Cancelled", "value": "4 (33.3%)"}]}
    assert unverified_numbers(draft, facts) == ["4500"]
