"""Report API endpoints used by the Angular ai_box module."""

from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import Response

from app.models.report_schema import (
    ErrorResponse,
    ReportDocument,
    ReportRequest,
    ReportResponse,
    ReportTypeInfo,
    SaveReportRequest,
    RenameReportRequest,
)
from app.services.deepseek_service import DeepSeekService
from app.services.report_service import DOCX_MEDIA_TYPE, PDF_MEDIA_TYPE, ReportService
from app.storage.database import storage
from fastapi import HTTPException

router = APIRouter(prefix="/api/reports", tags=["Reports"])

_VALIDATION_ERRORS = {
    400: {"model": ErrorResponse, "description": "Unsupported report type"},
    413: {"model": ErrorResponse, "description": "Request body too large"},
    422: {"model": ErrorResponse, "description": "Invalid request body or report data"},
}


@lru_cache
def get_report_service() -> ReportService:
    return ReportService(DeepSeekService())


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
) -> ReportResponse:
    import json
    import logging
    _log = logging.getLogger("ai_box")
    _log.info("generate_report called: report_type=%s, prompt=%s, data_keys=%s", request.report_type, request.prompt[:50], list(request.data.keys()) if isinstance(request.data, dict) else type(request.data))
    if isinstance(request.data, dict):
        for k in ("getOrderList", "orders", "data"):
            if k in request.data:
                v = request.data[k]
                _log.info("request.data['%s']: type=%s, len=%s", k, type(v), len(v) if isinstance(v, (list, dict)) else 'N/A')
    report = await service.generate_report(request)
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
def get_overview() -> dict:
    return storage.get_analytics_overview()


@router.post("/save", summary="Save generated report to database and generate export documents")
def save_report(
    req: SaveReportRequest,
    service: ReportService = Depends(get_report_service),
) -> dict:
    report_dict = req.report
    if not report_dict:
        try:
            report_obj = service.get_report(req.report_id, None)
            report_dict = report_obj.model_dump(mode="json")
        except Exception:
            existing = storage.get_report(req.report_id)
            if existing and existing.get("report"):
                report_dict = existing["report"]
            else:
                raise HTTPException(status_code=404, detail="Report data not found to save")

    doc = ReportDocument.model_validate(report_dict)
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

    saved = storage.save_report(
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
    return {"success": True, "saved_report": saved}


@router.get("/saved", summary="List saved reports with filtering and pagination")
def list_saved_reports(
    search: str = Query("", description="Search term for report name or description"),
    type: str = Query("", description="Report type filter"),
    page: int = Query(1, ge=1, description="Page number"),
    pageSize: int = Query(10, ge=1, le=100, description="Page size"),
    createdBy: str = Query("", description="Created by filter"),
) -> dict:
    return storage.list_reports(search=search, report_type=type, created_by=createdBy, page=page, page_size=pageSize)


@router.get("/saved/{report_id}", summary="Get a saved report by ID")
def get_saved_report(report_id: str) -> dict:
    record = storage.get_report(report_id)
    if not record:
        raise HTTPException(status_code=404, detail="Saved report not found")
    return record


@router.patch("/saved/{report_id}", summary="Rename or update description of a saved report")
def update_saved_report(report_id: str, req: RenameReportRequest) -> dict:
    updated = storage.update_report(report_id, name=req.name, description=req.description)
    if not updated:
        raise HTTPException(status_code=404, detail="Saved report not found")
    return updated


@router.delete("/saved/{report_id}", summary="Delete a saved report and stored files")
def delete_saved_report(report_id: str) -> dict:
    deleted = storage.delete_report(report_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Saved report not found")
    return {"success": True, "message": "Report deleted successfully"}


@router.get("/saved/{report_id}/download", summary="Download stored PDF or DOCX file of a saved report")
def download_saved_file(
    report_id: str,
    format: str = Query("pdf", pattern="^(pdf|docx)$"),
    service: ReportService = Depends(get_report_service),
) -> Response:
    content, filename = storage.get_file_content(report_id, format)
    if not content:
        record = storage.get_report(report_id)
        if not record or not record.get("report"):
            raise HTTPException(status_code=404, detail="Report document not found")
        doc = ReportDocument.model_validate(record["report"])
        content, filename = service.export_pdf(doc) if format == "pdf" else service.export_docx(doc)
    return _file_response(content, filename, PDF_MEDIA_TYPE if format == "pdf" else DOCX_MEDIA_TYPE)


@router.get("/{report_id}", response_model=ReportResponse)
def get_report(report_id: str, authorization: str | None = Header(default=None), service: ReportService = Depends(get_report_service)):
    return service.get_report(report_id, authorization)


@router.get("/{report_id}/download")
def download_report(report_id: str, format: str = Query(pattern="^(pdf|docx)$"),
                    authorization: str | None = Header(default=None), service: ReportService = Depends(get_report_service)):
    report = service.get_report(report_id, authorization)
    content, filename = service.export_pdf(report) if format == "pdf" else service.export_docx(report)
    return _file_response(content, filename, PDF_MEDIA_TYPE if format == "pdf" else DOCX_MEDIA_TYPE)
