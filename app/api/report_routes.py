"""Report API endpoints used by the Angular ai_box module."""

from __future__ import annotations

import asyncio
import logging
import time
from functools import lru_cache

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import Response

from app.api.auth import CurrentUser, get_optional_user, get_user_reports
from app.config import settings
from app.errors import ReportDeadlineError
from app.models.report_schema import (
    ErrorResponse,
    ReportDocument,
    ReportRequest,
    ReportResponse,
    ReportTypeInfo,
    SaveReportRequest,
    RenameReportRequest,
    ValidatePromptRequest,
    ValidatePromptResponse,
)
from app.services.deepseek_service import DeepSeekService
from app.services.prompt_validator_service import PromptValidatorService
from app.services.report_service import DOCX_MEDIA_TYPE, PDF_MEDIA_TYPE, ReportService
from app.storage.user_reports import UserReports
from fastapi import HTTPException

router = APIRouter(prefix="/api/reports", tags=["Reports"])
logger = logging.getLogger("ai_box")

_VALIDATION_ERRORS = {
    400: {"model": ErrorResponse, "description": "Unsupported report type"},
    413: {"model": ErrorResponse, "description": "Request body too large"},
    422: {"model": ErrorResponse, "description": "Invalid request body or report data"},
}


@lru_cache
def get_report_service() -> ReportService:
    return ReportService(DeepSeekService())


@lru_cache
def get_prompt_validator_service() -> PromptValidatorService:
    return PromptValidatorService(DeepSeekService())


@router.post(
    "/validate-prompt",
    response_model=ValidatePromptResponse,
    summary="Validate prompt and generate smart suggestions using DeepSeek",
)
async def validate_prompt(
    request: ValidatePromptRequest,
    validator: PromptValidatorService = Depends(get_prompt_validator_service),
) -> ValidatePromptResponse:
    return await validator.validate_prompt(request)


def _file_response(content: bytes, filename: str, media_type: str) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/types", response_model=list[ReportTypeInfo], summary="List supported report types")
async def list_report_types(service: ReportService = Depends(get_report_service)) -> list[ReportTypeInfo]:
    return service.list_report_types()


@router.post(
    "/generate",
    response_model=ReportResponse,
    summary="Generate an AI report from JSON data",
    responses={
        **_VALIDATION_ERRORS,
        502: {"model": ErrorResponse, "description": "AI service error or invalid AI response"},
        503: {"model": ErrorResponse, "description": "AI service not configured or unavailable"},
        504: {"model": ErrorResponse, "description": "AI service timeout"},
    },
)
async def generate_report(
    request: ReportRequest,
    response: Response,
    service: ReportService = Depends(get_report_service),
    user: CurrentUser | None = Depends(get_optional_user),
    x_shipment_authorization: str | None = Header(default=None, alias="X-Shipment-Authorization", description=(
        "source=shipment_api only: 'Bearer <BigShip login token>'. Forwarded unchanged as Authorization to the "
        "configured shipment API only; never logged, stored or sent to the AI model.")),
) -> ReportResponse:
    started = time.monotonic()
    if request.source == "shipment_api" and user is None:
        # AI Box login first; the shipment credential is checked separately (shipment_auth_required).
        raise HTTPException(status_code=401, detail="AI Box login is required: the X-User-Id header is missing.")
    logger.info("generate_report: report_type=%s source=%s prompt_chars=%s", request.report_type, request.source,
                len(request.prompt or ""))
    try:
        async with asyncio.timeout(settings.report_deadline_seconds) as deadline:
            report = await service.generate_report(request, shipment_credential=x_shipment_authorization,
                                                   user_id=user.id if user else None)
    except TimeoutError:
        if not deadline.expired():
            raise
        raise ReportDeadlineError("The report took too long to create. Please try again, or select a shorter date "
                                  "range.", details={"deadline_seconds": settings.report_deadline_seconds}) from None
    logger.info("generate_report done: source=%s records=%s ai_calls=%s ms=%d", request.source,
                report.records_analyzed, (report.generation or {}).get("ai_calls"), (time.monotonic() - started) * 1000)
    response.headers["X-Report-Access-Token"] = service.store_report(report)
    response.headers["Cache-Control"] = "no-store"
    return report


@router.post(
    "/export/docx",
    response_class=Response,
    summary="Export a generated report as a Word document",
    responses={
        200: {"content": {DOCX_MEDIA_TYPE: {}}, "description": "Word document"},
        **_VALIDATION_ERRORS,
        500: {"model": ErrorResponse, "description": "Document generation failed"},
    },
)
def export_docx(document: ReportDocument, service: ReportService = Depends(get_report_service)) -> Response:
    content, filename = service.export_docx(document)
    return _file_response(content, filename, DOCX_MEDIA_TYPE)


@router.post(
    "/export/pdf",
    response_class=Response,
    summary="Export a generated report as a PDF document",
    responses={
        200: {"content": {PDF_MEDIA_TYPE: {}}, "description": "PDF document"},
        **_VALIDATION_ERRORS,
        500: {"model": ErrorResponse, "description": "Document generation failed"},
    },
)
def export_pdf(document: ReportDocument, service: ReportService = Depends(get_report_service)) -> Response:
    content, filename = service.export_pdf(document)
    return _file_response(content, filename, PDF_MEDIA_TYPE)


@router.get("/overview", summary="Get real-time AI Box dashboard analytics overview")
def get_overview(reports: UserReports = Depends(get_user_reports)) -> dict:
    return reports.overview()


@router.post("/save", summary="Save generated report to database and generate export documents")
def save_report(
    req: SaveReportRequest,
    reports: UserReports = Depends(get_user_reports),
    service: ReportService = Depends(get_report_service),
) -> dict:
    report_dict = req.report
    if not report_dict:
        try:
            report_obj = service.get_report(req.report_id, None)
            report_dict = report_obj.model_dump(mode="json")
        except Exception:
            existing = reports.get(req.report_id)
            if existing and existing.get("report"):
                report_dict = existing["report"]
            else:
                raise HTTPException(status_code=404, detail="Report data not found to save")

    doc = ReportDocument.model_validate(report_dict)
    if doc.report_type == "shipment":
        from app.analysis.accuracy import verify_revision
        verify_revision(ReportResponse.model_validate(report_dict))
    pdf_bytes = None
    docx_bytes = None
    try:
        pdf_bytes, _ = service.export_pdf(doc)
    except Exception:
        pass
    try:
        docx_bytes, _ = service.export_docx(doc)
    except Exception:
        pass

    saved = reports.save(
        report_id=req.report_id,
        name=req.name,
        description=req.description,
        report_type=doc.report_type,
        prompt=doc.prompt,
        records_analyzed=doc.records_analyzed,
        report_data=report_dict,
        pdf_bytes=pdf_bytes,
        docx_bytes=docx_bytes,
    )
    if saved is None:
        raise HTTPException(status_code=409, detail="This report ID cannot be saved. Generate the report again.")
    return {"success": True, "saved_report": saved}


@router.get("/saved", summary="List saved reports with filtering and pagination")
def list_saved_reports(
    search: str = Query("", description="Search term for report name or description"),
    type: str = Query("", description="Report type filter"),
    page: int = Query(1, ge=1, description="Page number"),
    pageSize: int = Query(10, ge=1, le=100, description="Page size"),
    reports: UserReports = Depends(get_user_reports),
) -> dict:
    return reports.list(search=search, report_type=type, page=page, page_size=pageSize)


@router.get("/saved/{report_id}", summary="Get a saved report by ID")
def get_saved_report(report_id: str, reports: UserReports = Depends(get_user_reports)) -> dict:
    record = reports.get(report_id)
    if not record:
        raise HTTPException(status_code=404, detail="Saved report not found")
    return record


@router.patch("/saved/{report_id}", summary="Rename or update description of a saved report")
def update_saved_report(report_id: str, req: RenameReportRequest, reports: UserReports = Depends(get_user_reports)) -> dict:
    updated = reports.rename(report_id, name=req.name, description=req.description)
    if not updated:
        raise HTTPException(status_code=404, detail="Saved report not found")
    return updated


@router.delete("/saved/{report_id}", summary="Delete a saved report and stored files")
def delete_saved_report(report_id: str, reports: UserReports = Depends(get_user_reports)) -> dict:
    deleted = reports.delete(report_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Saved report not found")
    return {"success": True, "message": "Report deleted successfully"}


@router.get("/saved/{report_id}/download", summary="Download stored PDF or DOCX file of a saved report")
def download_saved_file(
    report_id: str,
    format: str = Query("pdf", pattern="^(pdf|docx)$"),
    reports: UserReports = Depends(get_user_reports),
    service: ReportService = Depends(get_report_service),
) -> Response:
    content, filename = reports.file(report_id, format)
    if not content:
        record = reports.get(report_id)
        if not record or not record.get("report"):
            raise HTTPException(status_code=404, detail="Report document not found")
        doc = ReportDocument.model_validate(record["report"])
        content, filename = service.export_pdf(doc) if format == "pdf" else service.export_docx(doc)
    return _file_response(content, filename, PDF_MEDIA_TYPE if format == "pdf" else DOCX_MEDIA_TYPE)


@router.get("/{report_id}", response_model=ReportResponse)
def get_report(report_id: str, authorization: str | None = Header(default=None),
               user: CurrentUser | None = Depends(get_optional_user), service: ReportService = Depends(get_report_service)):
    return service.get_report(report_id, authorization, owner_id=user.id if user else None)


@router.get("/{report_id}/download")
def download_report(report_id: str, format: str = Query(pattern="^(pdf|docx)$"),
                    authorization: str | None = Header(default=None), user: CurrentUser | None = Depends(get_optional_user),
                    service: ReportService = Depends(get_report_service)):
    report = service.get_report(report_id, authorization, owner_id=user.id if user else None)
    content, filename = service.export_pdf(report) if format == "pdf" else service.export_docx(report)
    return _file_response(content, filename, PDF_MEDIA_TYPE if format == "pdf" else DOCX_MEDIA_TYPE)
