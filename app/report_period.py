"""Resolve UI date presets once, using the selected local calendar and inclusive days."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def local_today(timezone: str) -> date:
    try:
        return datetime.now(ZoneInfo(timezone)).date()
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Unknown timezone '{timezone}'") from None


def preset_days(label: str | None) -> int | None:
    if label is None or label.strip().lower() in {"", "custom", "custom range", "custom_range"}:
        return None
    match = re.fullmatch(r"(?:last[\s_-]*)?(\d+)[\s_-]*(?:days?|d)?", label.strip(), re.I)
    if not match or int(match[1]) < 1:
        raise ValueError("date_range must be 'Last N Days', 'last_N_days', or 'custom'")
    return int(match[1])


def last_days_period(days: int, timezone: str, *, today: date | None = None) -> tuple[date, date]:
    if days < 1 or days > 3660:
        raise ValueError("range_days must be between 1 and 3660")
    end = today if today is not None else local_today(timezone)
    return end - timedelta(days=days - 1), end


def period_label(period: dict | None) -> str | None:
    if not period or not period.get("from_date") or not period.get("to_date"):
        return None
    field = period.get("date_field", "order_date").replace("_", " ")
    return f"{period['from_date']} to {period['to_date']} ({field}; {period.get('timezone', 'Asia/Kolkata')})"
