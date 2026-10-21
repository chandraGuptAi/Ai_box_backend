"""API endpoints for AI Insights compatibility (/api/ai-insights).

Directly connects the Angular OverviewComponent, SavedReportsComponent,
and ReportDetailsComponent to the real backend database storage and PDF engine.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.api.auth import get_optional_user_reports, get_user_reports
from app.api.report_routes import get_report_service, _file_response
from app.models.report_schema import ReportDocument, ReportResponse
from app.services.report_service import PDF_MEDIA_TYPE, ReportService
from app.storage.user_reports import UserReports

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
def get_insights_overview(reports: UserReports | None = Depends(get_optional_user_reports)) -> dict[str, Any]:
    if not reports:
        return {
            "totalReports": 0,
            "reportsThisMonth": 0,
            "monthlyChangePercent": None,
            "availableRecords": 0,
            "lastReport": None,
            "recentReports": [],
            "source": "empty",
        }
    return reports.overview()


@router.get("/reports", summary="List saved reports formatted for InsightsApiService")
def list_insights_reports(
    search: str = Query("", description="Search term"),
    reportType: str = Query("", description="Report type"),
    page: int = Query(1, ge=1),
    pageSize: int = Query(10, ge=1, le=100),
    reports: UserReports = Depends(get_user_reports),
) -> dict[str, Any]:
    result = reports.list(search=search, report_type=reportType, page=page, page_size=pageSize)
    angular_items = [reports.to_angular(item) for item in result["items"]]
    return {
        "items": angular_items,
        "total": result["total"],
        "page": result["page"],
        "pageSize": result["pageSize"],
        "creators": [{"id": reports.user_id, "name": reports.user_name}],
    }


@router.get("/reports/{report_id}", summary="Get a saved report for ReportDetailsComponent")
def get_insights_report(report_id: str, reports: UserReports = Depends(get_user_reports)) -> dict[str, Any]:
    record = reports.get(report_id)
    if not record:
        raise HTTPException(status_code=404, detail="Saved report not found")
    return reports.to_angular(record)


@router.post("/reports", summary="Save a report via InsightsApiService")
def save_insights_report(
    req: SaveInsightsReportRequest,
    reports: UserReports = Depends(get_user_reports),
    service: ReportService = Depends(get_report_service),
) -> dict[str, Any]:
    # Try fetching from in-memory or storage
    report_dict = None
    try:
        rep = service.get_report(req.analysisId, None)
        report_dict = rep.model_dump(mode="json")
    except Exception:
        existing = reports.get(req.analysisId)
        if existing and existing.get("report"):
            report_dict = existing["report"]

    if not report_dict:
        raise HTTPException(status_code=404, detail=f"Analysis report '{req.analysisId}' not found to save.")

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
    if saved is None:
        raise HTTPException(status_code=409, detail="This report ID cannot be saved. Generate the report again.")
    return reports.to_angular(saved)


@router.patch("/reports/{report_id}", summary="Rename a report via InsightsApiService")
def rename_insights_report(report_id: str, req: RenameInsightsReportRequest,
                           reports: UserReports = Depends(get_user_reports)) -> dict[str, Any]:
    updated = reports.rename(report_id, name=req.name, description=req.description)
    if not updated:
        raise HTTPException(status_code=404, detail="Saved report not found")
    return reports.to_angular(updated)


@router.delete("/reports/{report_id}", summary="Delete a report via InsightsApiService")
def delete_insights_report(report_id: str, reports: UserReports = Depends(get_user_reports)) -> dict[str, Any]:
    deleted = reports.delete(report_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Saved report not found")
    return {"success": True}


@router.get("/reports/{report_id}/export", summary="Export saved report as PDF for InsightsApiService")
def export_insights_pdf(
    report_id: str,
    reports: UserReports = Depends(get_user_reports),
    service: ReportService = Depends(get_report_service),
) -> Response:
    content, filename = reports.file(report_id, "pdf")
    if not content:
        record = reports.get(report_id)
        if not record or not record.get("report"):
            raise HTTPException(status_code=404, detail="Report document not found")
        doc = ReportDocument.model_validate(record["report"])
        content, filename = service.export_pdf(doc)
    return _file_response(content, filename, PDF_MEDIA_TYPE)
