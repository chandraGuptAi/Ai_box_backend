"""Admin audit trail API (/api/admin/audit).

Every seller's prompts and company names are in the audit trail, so this API never uses the seller
login (X-User-Id). Each call needs the admin key from AI_BOX_AUDIT_API_KEY in the X-Audit-Key header;
with no key configured the API is disabled.
"""
from __future__ import annotations

import csv
import hmac
import io
import math
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response

from app.config import settings
from app.services.audit_trail import CSV_COLUMNS, query_report_audit, summarize

router = APIRouter(prefix="/api/admin/audit", tags=["Admin audit"])

MAX_RANGE_DAYS = 93


def require_audit_key(x_audit_key: str | None = Header(default=None, alias="X-Audit-Key")) -> None:
    expected = settings.audit_api_key
    if not expected:
        raise HTTPException(status_code=503, detail="The audit API is not enabled on this server.")
    if not x_audit_key or not hmac.compare_digest(x_audit_key.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="A valid X-Audit-Key header is required.")


def _today_ist() -> date:
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


@router.get("/reports", dependencies=[Depends(require_audit_key)],
            summary="Report generation audit trail (admin)")
def report_audit(
    from_date: date | None = Query(default=None, description="First IST day, YYYY-MM-DD. Default: today."),
    to_date: date | None = Query(default=None, description="Last IST day, inclusive. Default: from_date."),
    user_id: str | None = Query(default=None, max_length=100, description="Only this seller."),
    outcome: Literal["generated", "failed"] | None = Query(default=None),
    error_code: str | None = Query(default=None, max_length=60, description="e.g. no_matching_records"),
    search: str | None = Query(default=None, max_length=200, description="Text in prompt, company, user or customer ID."),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    format: Literal["json", "csv"] = Query(default="json", description="csv downloads every matching row."),
):
    start = from_date or _today_ist()
    end = to_date or (start if from_date else _today_ist())
    if end < start:
        raise HTTPException(status_code=400, detail="to_date must not be before from_date.")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(status_code=400, detail=f"Select at most {MAX_RANGE_DAYS} days.")
    items = query_report_audit(from_date=start, to_date=end, user_id=user_id, outcome=outcome,
                               error_code=error_code, search=search)
    if format == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(items)
        name = f"ai_box_audit_{start.isoformat()}_to_{end.isoformat()}.csv"
        return Response(content="﻿" + buffer.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})
    total = len(items)
    return {
        "filters": {"from_date": start.isoformat(), "to_date": end.isoformat(), "user_id": user_id,
                    "outcome": outcome, "error_code": error_code, "search": search, "timezone": "Asia/Kolkata"},
        "summary": summarize(items),
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": math.ceil(total / page_size) if total else 0,
        "items": items[(page - 1) * page_size: page * page_size],
    }
