"""API endpoints for AI Insights compatibility (/api/ai-insights).

Directly connects the Angular OverviewComponent, SavedReportsComponent,
and ReportDetailsComponent to the real backend database storage and PDF engine.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.api.report_routes import get_report_service, _file_response
from app.models.report_schema import ReportDocument
from app.services.report_service import PDF_MEDIA_TYPE, ReportService
from app.storage.database import storage

logger = logging.getLogger("ai_box.insights")

router = APIRouter(prefix="/api/ai-insights", tags=["AI Insights"])


class SaveInsightsReportRequest(BaseModel):
    analysisId: str
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)


class RenameInsightsReportRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)


@router.get("/overview", summary="Real-time report statistics for the AI Box Dashboard")
def get_insights_overview() -> dict[str, Any]:
    return storage.get_analytics_overview()


@router.get("/reports", summary="List saved reports formatted for InsightsApiService")
def list_insights_reports(
    search: str = Query("", description="Search term"),
    reportType: str = Query("", description="Report type"),
    createdBy: str = Query("", description="Creator"),
    page: int = Query(1, ge=1),
    pageSize: int = Query(10, ge=1, le=100),
) -> dict[str, Any]:
    result = storage.list_reports(search=search, report_type=reportType, created_by=createdBy, page=page, page_size=pageSize)
    angular_items = [storage._to_angular_saved_report(item) for item in result["items"]]
    return {
        "items": angular_items,
        "total": result["total"],
        "page": result["page"],
        "pageSize": result["pageSize"],
        "creators": [{"id": "user", "name": "Current User"}],
    }


@router.get("/reports/{report_id}", summary="Get a saved report for ReportDetailsComponent")
def get_insights_report(report_id: str) -> dict[str, Any]:
    record = storage.get_report(report_id)
    if not record:
        raise HTTPException(status_code=404, detail="Saved report not found")
    return storage._to_angular_saved_report(record)


@router.post("/reports", summary="Save a report via InsightsApiService")
def save_insights_report(
    req: SaveInsightsReportRequest,
    service: ReportService = Depends(get_report_service),
) -> dict[str, Any]:
    # Try fetching from in-memory or storage
    report_dict = None
    try:
        rep = service.get_report(req.analysisId, None)
        report_dict = rep.model_dump(mode="json")
    except Exception:
        existing = storage.get_report(req.analysisId)
        if existing and existing.get("report"):
            report_dict = existing["report"]

    if not report_dict:
        raise HTTPException(status_code=404, detail=f"Analysis report '{req.analysisId}' not found to save.")

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
        report_id=req.analysisId,
        name=req.name,
        description=req.description,
        report_type=doc.report_type,
        prompt=doc.prompt,
        records_analyzed=doc.records_analyzed,
        report_data=report_dict,
        pdf_bytes=pdf_bytes,
        docx_bytes=docx_bytes,
    )
    return storage._to_angular_saved_report(saved)


@router.patch("/reports/{report_id}", summary="Rename a report via InsightsApiService")
def rename_insights_report(report_id: str, req: RenameInsightsReportRequest) -> dict[str, Any]:
    updated = storage.update_report(report_id, name=req.name, description=req.description)
    if not updated:
        raise HTTPException(status_code=404, detail="Saved report not found")
    return storage._to_angular_saved_report(updated)


@router.delete("/reports/{report_id}", summary="Delete a report via InsightsApiService")
def delete_insights_report(report_id: str) -> dict[str, Any]:
    deleted = storage.delete_report(report_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Saved report not found")
    return {"success": True}


@router.get("/reports/{report_id}/export", summary="Export saved report as PDF for InsightsApiService")
def export_insights_pdf(
    report_id: str,
    service: ReportService = Depends(get_report_service),
) -> Response:
    content, filename = storage.get_file_content(report_id, "pdf")
    if not content:
        record = storage.get_report(report_id)
        if not record or not record.get("report"):
            raise HTTPException(status_code=404, detail="Report document not found")
        doc = ReportDocument.model_validate(record["report"])
        content, filename = service.export_pdf(doc)
    return _file_response(content, filename, PDF_MEDIA_TYPE)
