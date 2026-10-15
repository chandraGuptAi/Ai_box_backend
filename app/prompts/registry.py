"""Registry of supported report types.

To add a report type (e.g. courier performance):
1. Create ``app/prompts/courier_prompt.py`` exposing a ``ReportDefinition``
   (system prompt + an ``analyze(data)`` function returning ``AnalysisResult``).
2. Import it below and add it to ``_DEFINITIONS``.
No API, service or generator changes are required.
"""

from __future__ import annotations

from app.errors import UnsupportedReportTypeError
from app.prompts.base import ReportDefinition
from app.prompts.shipment_prompt import SHIPMENT_REPORT
from app.prompts.remittance_prompt import PAID_REMITTANCE_REPORT, PENDING_REMITTANCE_REPORT

_DEFINITIONS: tuple[ReportDefinition, ...] = (
    SHIPMENT_REPORT,
)

REPORT_DEFINITIONS: dict[str, ReportDefinition] = {
    SHIPMENT_REPORT.report_type: SHIPMENT_REPORT,
    "shipments": SHIPMENT_REPORT,
    PAID_REMITTANCE_REPORT.report_type: PAID_REMITTANCE_REPORT,
    "paid_remittance": PAID_REMITTANCE_REPORT,
    "paid-remittance": PAID_REMITTANCE_REPORT,
    PENDING_REMITTANCE_REPORT.report_type: PENDING_REMITTANCE_REPORT,
    "pending_remittance": PENDING_REMITTANCE_REPORT,
    "pending-remittance": PENDING_REMITTANCE_REPORT,
}


def get_report_definition(report_type: str) -> ReportDefinition:
    key = str(report_type).strip().lower().replace("-", "_")
    definition = REPORT_DEFINITIONS.get(key)
    if definition is None:
        raise UnsupportedReportTypeError(
            f"Unsupported report type '{report_type}'.",
            details={"supported_report_types": sorted(REPORT_DEFINITIONS)},
        )
    return definition


def list_report_definitions() -> list[ReportDefinition]:
    return list(_DEFINITIONS)
