"""Private seller report-generation history, separate from data-coverage diagnostics."""

import json
import logging
import threading
from logging.handlers import RotatingFileHandler

from app.config import BASE_DIR

_lock = threading.Lock()


def write_report_activity(record):
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
