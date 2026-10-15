"""Application error types and the JSON error format returned to clients.

Every error response has the shape::

    {"error": {"code": "...", "category": "...", "message": "...", "details": ...}}

``category`` lets the Angular client tell apart problems with the request
("validation"), failures while producing a report with the AI service
("report_generation"), document export failures ("export") and unexpected
server errors ("server").
"""

from __future__ import annotations

from typing import Any


def error_payload(code: str, category: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "category": category, "message": message, "details": details}}


class AppError(Exception):
    status_code = 500
    code = "internal_error"
    category = "server"

    def __init__(self, message: str, *, details: Any = None, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        if code:
            self.code = code

    def to_payload(self) -> dict[str, Any]:
        return error_payload(self.code, self.category, self.message, self.details)


# --- Validation errors (problems with what the client sent) -----------------

class UnsupportedReportTypeError(AppError):
    status_code = 400
    code = "unsupported_report_type"
    category = "validation"


class InvalidReportDataError(AppError):
    status_code = 422
    code = "invalid_report_data"
    category = "validation"


class InvalidPromptError(AppError):
    status_code = 422
    code = "invalid_prompt"
    category = "validation"


class UnsupportedRequestError(InvalidPromptError):
    """The request is clear, but the supplied data cannot answer it (details name what is missing)."""
    code = "unsupported_request"


# --- Report generation errors (AI service side) -----------------------------

class AIConfigurationError(AppError):
    status_code = 503
    code = "ai_not_configured"
    category = "report_generation"


class AIServiceError(AppError):
    status_code = 502
    code = "ai_service_error"
    category = "report_generation"


class AIServiceTimeoutError(AIServiceError):
    status_code = 504
    code = "ai_timeout"


class AIServiceUnavailableError(AIServiceError):
    status_code = 503
    code = "ai_unavailable"


class AIResponseError(AppError):
    status_code = 502
    code = "ai_invalid_response"
    category = "report_generation"


# --- Export errors ----------------------------------------------------------

class ReportExportError(AppError):
    status_code = 500
    code = "report_export_failed"
    category = "export"
