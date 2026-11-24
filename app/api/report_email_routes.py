"""Email a report ("Send to" Gmail / Outlook in the Angular report page).

POST /api/reports/{id}/email/compose
    The AI writes the email from the user's original report request plus the report's validated results (it
    describes them, never recalculates), the backend renders it to HTML and attaches the report PDF, and
    Ai_Box_Service creates it as a DRAFT in the user's connected mailbox. The response's ``open_url`` opens
    that draft in Gmail / Outlook on the web, where the user adds recipients and sends it. AI Box never sends.

The report is resolved like downloads are: its access token (``Authorization: Bearer <report token>``) or the
owner's saved copy. When the server no longer has it (generated reports live in memory and are lost on a
restart), the page may send the report itself, as Export does; the PDF export then verifies that this exact
report revision is unchanged before anything else happens. The mailbox is resolved by Ai_Box_Service from the user's BigShip login, so a user can
only use mailboxes their own seller account connected. Connecting a mailbox goes through the gateway route
/api/email (Ai_Box_Service).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.api.auth import CurrentUser, get_current_user
from app.api.report_routes import get_report_service
from app.config import settings
from app.models.report_schema import ReportResponse
from app.services import report_memory
from app.services.ai_box_email import create_draft
from app.services.deepseek_service import DeepSeekService
from app.services.report_email import Tone, default_language, email_content, render_email, report_title
from app.services.report_service import ReportService
from app.services.shipment_source import delegated_credential

router = APIRouter(prefix="/api/reports", tags=["Report email"])


@lru_cache
def get_email_ai() -> DeepSeekService | None:
    return DeepSeekService() if settings.deepseek_configured else None


class ComposeRequest(BaseModel):
    connection_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$", description="From /api/email/providers")
    tone: Tone = "professional"
    language: Literal["en", "hi"] | None = Field(default=None, description="Default: the report request's language")
    instruction: str = Field(default="", max_length=500)
    report: ReportResponse | None = Field(default=None, description="Only when the server answered 404 for the id")


class ComposeResponse(BaseModel):
    draft_id: str
    open_url: str = Field(description="Open in a new tab: the draft in Gmail / Outlook on the web")
    provider: Literal["gmail", "outlook"]
    from_email: str
    subject: str
    attachment_name: str | None = None
    content_source: Literal["ai", "report"] = Field(
        description="ai: written by the AI; report: built from the report text (AI unavailable or not trusted)")


@router.post("/{report_id}/email/compose", response_model=ComposeResponse,
             summary="Create this report's email (AI body + PDF) as a draft in the user's Gmail/Outlook")
async def compose(report_id: str, body: ComposeRequest, background: BackgroundTasks,
                  authorization: str | None = Header(default=None),
                  x_shipment_authorization: str | None = Header(default=None, alias="X-Shipment-Authorization"),
                  user: CurrentUser = Depends(get_current_user),
                  service: ReportService = Depends(get_report_service),
                  ai: DeepSeekService | None = Depends(get_email_ai)):
    login = delegated_credential(x_shipment_authorization)   # 401 if the BigShip login is missing/unreadable
    try:
        report = service.get_report(report_id, authorization, owner_id=user.id)
    except HTTPException as exc:
        if exc.status_code != 404 or body.report is None or body.report.report_id != report_id:
            raise
        report = body.report
    # The PDF first: for a report sent by the page, this is where its revision is verified.
    pdf_bytes, filename = await run_in_threadpool(service.export_pdf, report)
    content, source = await email_content(ai, report, tone=body.tone, instruction=body.instruction,
                                          language=body.language or default_language(report))
    html, text = render_email(content)
    draft = await create_draft({
        "connection_id": body.connection_id, "subject": content.subject, "html": html, "text": text,
        "report_id": report_id, "user_id": user.id,
    }, (filename or f"{report_title(report)}.pdf", pdf_bytes), login, user.id)
    background.add_task(report_memory.send_feedback, report_id, "shared")
    return {**draft, "content_source": source}
