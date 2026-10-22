"""Indian calendar and epoch-second conversion rules, independent of server timezone."""

from __future__ import annotations

from datetime import datetime, date, timedelta, timezone
import re
import time
from ..errors import EscalationError

IST = timezone(timedelta(hours=5, minutes=30))


def day(ts: int | float) -> str:
    return datetime.fromtimestamp(ts, IST).date().isoformat()


def midnight(value: str) -> int:
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError()
        return int(datetime.combine(date.fromisoformat(value), datetime.min.time(), IST).timestamp())
    except (ValueError, TypeError):
        raise EscalationError(400, "Choose valid start and end dates (YYYY-MM-DD).")


def date_filter(start: str | None, end: str | None) -> dict:
    if not start and not end:
        return {}
    if not start or not end:
        raise EscalationError(400, "Choose valid start and end dates together.")
    first, last = midnight(start), midnight(end) + 86400
    if first >= last:
        raise EscalationError(400, "Choose valid start and end dates in order.")
    return {"from": first, "to": last}


def resolve_window(options: dict | None = None, now: int | None = None) -> dict:
    options = options or {}
    now = int(time.time()) if now is None else now
    if options.get("from") is not None and options.get("to") is not None:
        start, end = int(options["from"]), int(options["to"])
    elif options.get("fromDate") or options.get("toDate"):

        def parse(value: str, end: bool = False) -> int:
            if not value:
                raise EscalationError(400, "Choose both start and end dates.")
            if "T" not in value:
                return midnight(value) + (86399 if end else 0)
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", value):
                    raise ValueError()
                return int(datetime.fromisoformat(value).replace(tzinfo=IST).timestamp())
            except ValueError:
                raise EscalationError(400, "Invalid IST date/time.")

        start, end = parse(options.get("fromDate")), parse(options.get("toDate"), True)
    elif options.get("mode") in ("today-so-far", "previous-day"):
        today = midnight(day(now))
        start, end = (today, now) if options["mode"] == "today-so-far" else (today - 86400, today - 1)
    else:
        hours = options.get("hours") or 24
        if not isinstance(hours, (int, float)) or hours <= 0 or hours > 8760:
            raise EscalationError(400, "Invalid analysis hours.")
        return {"from": int(now - hours * 3600), "to": now, "label": f"last {hours}h", "rolling": True}
    if start > end:
        raise EscalationError(400, "Choose valid start and end dates in order.")
    label = options.get("label") or (
        f"{options['fromDate'].replace('T',' ')} to {options['toDate'].replace('T',' ')}"
        if options.get("fromDate")
        else day(start)
    )
    if options.get("mode") == "today-so-far":
        label += " (so far)"
    return {"from": start, "to": end, "label": label}


def format_time(ts: int | None) -> str:
    return "Not available" if ts is None else datetime.fromtimestamp(ts, IST).strftime("%d %b %Y, %H:%M IST")
