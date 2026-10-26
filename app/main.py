"""FastAPI application entry point.

Run with:  uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.report_routes import router as report_router
from app.config import settings
from app.errors import AppError, error_payload, request_id_var
from app.middleware import RequestIdMiddleware, RequestSizeLimitMiddleware
from app.security.dependencies import privacy_request_context

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("ai_box")

app = FastAPI(
    dependencies=[Depends(privacy_request_context)],
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "AI-powered report generation for the AI Box Angular module. "
        "Generates structured reports with DeepSeek and exports them as DOCX or PDF.\n\n"
        "Stored reports require the per-report bearer access token returned on generation."
    ),
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
)

# Middleware added last runs first: CORS wraps the size limit so 413 responses carry CORS headers,
# and the request ID wraps both so every error body names its request.
app.add_middleware(RequestSizeLimitMiddleware, max_bytes=settings.max_request_bytes)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_allowed_origins),
    allow_origin_regex=r"^https?://.*$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_private_network=True,
    expose_headers=["Content-Disposition", "X-Report-Access-Token", "X-Request-Id"],
)


@app.exception_handler(AppError)
async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
    if exc.status_code >= 500 or exc.category == "source":
        logger.warning("%s (%s): %s", exc.code, exc.status_code, exc.message)
    headers = None
    retry_after = (exc.details or {}).get("retry_after_seconds") if isinstance(exc.details, dict) else None
    if isinstance(retry_after, int):
        headers = {"Retry-After": str(retry_after)}
    return JSONResponse(status_code=exc.status_code, content=exc.to_payload(), headers=headers)


@app.exception_handler(RequestValidationError)
async def handle_request_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    # Input values are deliberately not echoed back (they may be large or contain personal data).
    details = [
        {
            "field": ".".join(str(part) for part in error.get("loc", ())),
            "message": error.get("msg", ""),
            "type": error.get("type", ""),
        }
        for error in exc.errors()[:50]
    ]
    return JSONResponse(
        status_code=422,
        content=error_payload("request_validation_error", "validation", "The request body is invalid.", details),
    )


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    # 401 here is always the AI Box login (X-User-Id / report token), never the shipment service.
    codes = {401: "ai_box_auth_required", 404: "not_found", 405: "method_not_allowed", 413: "request_too_large"}
    category = "validation" if 400 <= exc.status_code < 500 else "server"
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(codes.get(exc.status_code, "http_error"), category, str(exc.detail),
                              retryable=exc.status_code in (503, 504)),
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(Exception)
async def handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled server error (request_id=%s)", request_id_var.get(), exc_info=exc)
    return JSONResponse(
        status_code=500,
        content=error_payload("internal_error", "server", "An unexpected server error occurred."),
    )


@app.get("/health", tags=["Health"], summary="Health check")
async def health() -> dict:
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "ai_configured": settings.deepseek_configured,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


from app.api.insights_routes import router as insights_router
from app.api.skill_routes import router as skill_router, prompt_router

app.include_router(report_router)
app.include_router(insights_router)
app.include_router(skill_router)
app.include_router(prompt_router)
from app.api.canonical_report_routes import router as canonical_report_router
app.include_router(canonical_report_router)
from app.api.audit_routes import router as audit_router
app.include_router(audit_router)

# External service routes are registered in the central API gateway.
from app.api_gateway import install as install_api_gateway
install_api_gateway(app)
