import asyncio
import json
import logging
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.models.report_schema import ReportRequest
from app.services import report_activity, report_service


@pytest.mark.parametrize("fails", [False, True])
def test_generation_tracks_seller_and_result(monkeypatch, fails):
    records = []
    monkeypatch.setattr(report_service, "write_report_activity", records.append)
    service = report_service.ReportService(SimpleNamespace(model="fake-model"))
    result = SimpleNamespace(report_id="r1", customer_id="c1", records_analyzed=12, generation={"ai_calls": 1})

    async def generate(*args, **kwargs):
        if fails:
            raise ValueError("private error")
        return result

    monkeypatch.setattr(service, "_generate_report", generate)
    request = ReportRequest.model_validate({"report_type": "shipment", "data": {"getOrderList": []},
                                            "prompt": "Seller report"})
    if fails:
        with pytest.raises(ValueError):
            asyncio.run(service.generate_report(request, user_id="s1", shipment_credential="secret-token"))
    else:
        assert asyncio.run(service.generate_report(request, user_id="s1")) is result
    assert len(records) == 1
    record = records[0]
    assert record["seller_id"] == record["user_id"] == "s1"
    assert record["prompt"] == "Seller report"
    assert "company_name" in record and "filters" in record
    assert record["outcome"] == ("failed" if fails else "generated")
    assert datetime.fromisoformat(record["completed_at"]) >= datetime.fromisoformat(record["started_at"])
    assert record["request_id"] and record["report_id"]
    assert "secret-token" not in json.dumps(record)
    assert "private error" not in json.dumps(record)


def test_activity_file(tmp_path, monkeypatch):
    monkeypatch.setattr(report_activity, "BASE_DIR", tmp_path)
    audit = logging.getLogger("ai_box.report_activity")
    saved, level, propagate = list(audit.handlers), audit.level, audit.propagate
    audit.handlers = []
    audit.setLevel(logging.CRITICAL)
    try:
        report_activity.write_report_activity({"seller_id": "s1", "prompt": "first\nsecond", "token": "secret"})
        lines = (tmp_path / "output/report_generation_audit.jsonl").read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == {"seller_id": "s1", "prompt": "first\nsecond"}   # unlisted keys never written
    finally:
        for handler in audit.handlers:
            handler.close()
        audit.handlers = saved
        audit.setLevel(level)
        audit.propagate = propagate
