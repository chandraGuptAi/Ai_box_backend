"""Private seller report-generation history, separate from data-coverage diagnostics."""

import json
import logging
import threading
from logging.handlers import RotatingFileHandler

from app.config import BASE_DIR
from app.services import audit_db

_lock = threading.Lock()


def write_report_activity(record):
    # Admin audit trail (read by /api/admin/audit with X-Audit-Key): who asked for which report, with what
    # prompt and filters, and the outcome. Credentials, shipment rows and exception text are never written.
    allowed = {"event", "request_id", "report_id", "report_type", "source", "started_at", "completed_at",
               "seller_id", "user_id", "customer_id", "company_name", "prompt", "filters", "date_range", "generation",
               "started_at_ist", "duration_ms", "outcome", "error_code", "allow_partial", "is_follow_up",
               "previous_report_id", "records_analyzed", "ai_calls", "reason", "source_records", "pincode_records",
               # Period and failure diagnostics: a keyed seller pseudonym, dates and fixed codes only.
               "seller_ref", "processing_stage", "date_range_source", "resolved_start_date", "resolved_end_date",
               "date_validation_result", "internal_error_code"}
    record = {key: value for key, value in record.items() if key in allowed}
    audit = logging.getLogger("ai_box.report_activity")
    try:
        with _lock:
            if not any(isinstance(h, RotatingFileHandler) for h in audit.handlers):
                directory = BASE_DIR / "output"
                directory.mkdir(parents=True, exist_ok=True)
                handler = RotatingFileHandler(directory / "report_generation_audit.jsonl",
                                              maxBytes=5_000_000, backupCount=5, encoding="utf-8")
                handler.setFormatter(logging.Formatter("%(message)s"))
                audit.addHandler(handler)
            audit.propagate = False
            # Write directly so application log-level settings cannot suppress history.
            handler = next(h for h in audit.handlers if isinstance(h, RotatingFileHandler))
            handler.handle(logging.LogRecord(audit.name, logging.INFO, __file__, 0,
                                            json.dumps(record, ensure_ascii=True), (), None))
    except OSError:
        logging.getLogger(__name__).warning("Report activity file unavailable; request_id=%s",
                                            record.get("request_id"))
    audit_db.submit("generation", record)
