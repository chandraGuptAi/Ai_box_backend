"""Shared fixtures: coverage audit records are captured in memory instead of written to output/."""
from __future__ import annotations

import pytest

from app.services import report_service
from app.config import settings


@pytest.fixture(autouse=True)
def legacy_report_mode():
    """Legacy regressions explicitly exercise the supported rule-only mode.

    Ordered-workflow tests instantiate Settings(report_ai_planner=True) explicitly.
    """
    previous = settings.report_ai_planner
    object.__setattr__(settings, "report_ai_planner", False)
    yield
    object.__setattr__(settings, "report_ai_planner", previous)

# The real writer, for the test that checks the audit file itself.
real_write_coverage_audit = report_service.write_coverage_audit


@pytest.fixture(autouse=True)
def coverage_audit(monkeypatch):
    records: list[dict] = []
    monkeypatch.setattr(report_service, "write_coverage_audit", records.append)
    return records
