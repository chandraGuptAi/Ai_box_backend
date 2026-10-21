"""Application error types and the JSON error format returned to clients.

Every error response has the shape::

    {"error": {"code": "...", "category": "...", "message": "...", "details": ...,
               "retryable": false, "request_id": "..."}}

``category`` lets the Angular client tell apart problems with the request
("validation"), failures while producing a report with the AI service
("report_generation"), failures of the shipment source ("source"), document
export failures ("export") and unexpected server errors ("server").
``retryable`` says whether sending the same request again can succeed;
``request_id`` matches the ``X-Request-Id`` response header and the server logs.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

# Set per request by RequestIdMiddleware.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
report_id_var: ContextVar[str | None] = ContextVar("report_id", default=None)


def error_payload(code: str, category: str, message: str, details: Any = None, *,
                  retryable: bool = False) -> dict[str, Any]:
    return {"error": {"code": code, "category": category, "message": message, "details": details,
                      "retryable": retryable, "request_id": request_id_var.get()}}


class AppError(Exception):
    status_code = 500
    code = "internal_error"
    category = "server"
    retryable = False

    def __init__(self, message: str, *, details: Any = None, code: str | None = None,
                 status_code: int | None = None, retryable: bool | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        if code:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        if retryable is not None:
            self.retryable = retryable

    def to_payload(self) -> dict[str, Any]:
        return error_payload(self.code, self.category, self.message, self.details, retryable=self.retryable)


# --- Validation errors (problems with what the client sent) -----------------

class UnsupportedReportTypeError(AppError):
    status_code = 400
    code = "unsupported_report_type"
    category = "validation"


class InvalidReportDataError(AppError):
    status_code = 422
    code = "invalid_report_data"
    category = "validation"


class ReportAccuracyError(AppError):
    status_code = 422
    code = "report_accuracy_failed"
    category = "report_generation"


class ReportRevisionError(AppError):
    status_code = 409
    code = "report_revision_mismatch"
    category = "export"


class InvalidPromptError(AppError):
    status_code = 422
    code = "invalid_prompt"
    category = "validation"


class UnsupportedRequestError(InvalidPromptError):
    """The request is clear, but the supplied data cannot answer it (details name what is missing)."""
    code = "unsupported_request"


class ClarificationRequiredError(InvalidPromptError):
    """The prompt contradicts the selected filters; details list the conflict and the choices."""
    code = "clarification_required"


class IncompleteSourceError(InvalidReportDataError):
    """Fewer records were received than the source matched (e.g. only the first page)."""
    code = "incomplete_source"


class ScopeMismatchError(InvalidReportDataError):
    """Received records fall outside the selected filters: the source query did not apply them."""
    code = "scope_mismatch"


class ConflictingRecordsError(InvalidReportDataError):
    """The same shipment (order ID + AWB) arrived more than once with different values."""
    code = "conflicting_records"


class NoMatchingRecordsError(InvalidReportDataError):
    code = "no_matching_records"


class UnsupportedFilterError(InvalidReportDataError):
    """A selected filter value, date field or range the backend cannot send or verify."""
    code = "unsupported_filter"


class SourceTooLargeError(InvalidReportDataError):
    """More records match than one report may fetch; the user must narrow the filters."""
    code = "source_too_large"


# --- Shipment source errors (backend-owned fetch from the shipment API) ------

class ShipmentSourceError(AppError):
    status_code = 502
    code = "upstream_error"
    category = "source"


class ShipmentSourceNotConfiguredError(ShipmentSourceError):
    status_code = 503
    code = "shipment_source_not_configured"


class ShipmentAuthRequiredError(ShipmentSourceError):
    """No shipment-service credential came with the request (not an AI Box login failure)."""
    status_code = 401
    code = "shipment_auth_required"


class ShipmentAuthInvalidError(ShipmentSourceError):
    """X-Shipment-Authorization is present but not a usable "Bearer <token>" value."""
    status_code = 401
    code = "shipment_auth_invalid"


class UpstreamAuthError(ShipmentSourceError):
    """The shipment service rejected the delegated credential (expired or invalid)."""
    status_code = 401
    code = "upstream_auth_failed"


class UpstreamForbiddenError(ShipmentSourceError):
    status_code = 403
    code = "upstream_forbidden"


class UpstreamRateLimitedError(ShipmentSourceError):
    status_code = 429
    code = "upstream_rate_limited"
    retryable = True


class UpstreamTimeoutError(ShipmentSourceError):
    status_code = 504
    code = "upstream_timeout"
    retryable = True


class UpstreamUnavailableError(ShipmentSourceError):
    code = "upstream_unavailable"
    retryable = True


class UpstreamRejectedError(ShipmentSourceError):
    """The shipment service refused the request (4xx other than auth, or success=false)."""
    code = "upstream_rejected"


class UpstreamInvalidResponseError(ShipmentSourceError):
    code = "upstream_invalid_response"


class UpstreamPaginationError(ShipmentSourceError):
    """Repeated, empty or overlapping pages: the list could not be read consistently."""
    code = "upstream_pagination_inconsistent"
    retryable = True


class SourceChangedError(ShipmentSourceError):
    """The matching total changed while the list was being read."""
    status_code = 409
    code = "source_changed"
    retryable = True


class ReportDeadlineError(AppError):
    status_code = 504
    code = "report_deadline_exceeded"
    category = "server"
    retryable = True


# --- Report generation errors (AI service side) -----------------------------

class AIConfigurationError(AppError):
    status_code = 503
    code = "ai_not_configured"
    category = "report_generation"


class AIServiceError(AppError):
    status_code = 502
    code = "ai_service_error"
    category = "report_generation"
    retryable = True


class AIServiceTimeoutError(AIServiceError):
    status_code = 504
    code = "ai_timeout"
    retryable = True


class AIServiceUnavailableError(AIServiceError):
    status_code = 503
    code = "ai_unavailable"
    retryable = True


class AIResponseError(AppError):
    status_code = 502
    code = "ai_invalid_response"
    category = "report_generation"


# --- Export errors ----------------------------------------------------------

class ReportExportError(AppError):
    status_code = 500
    code = "report_export_failed"
    category = "export"
