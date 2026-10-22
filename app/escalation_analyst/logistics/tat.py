"""Never infer a verified response or closure from keyword matches."""

from __future__ import annotations

import math
from .awb import extract_awbs


def duration(start: int | float | None, end: int | float | None) -> int | float | None:
    return (
        end - start
        if isinstance(start, (int, float))
        and isinstance(end, (int, float))
        and math.isfinite(start)
        and math.isfinite(end)
        and end >= start
        else None
    )


def format_duration(seconds: int | float | None) -> str:
    if seconds is None or not math.isfinite(seconds):
        return "Not available"
    hours, minutes = math.floor(seconds / 3600), math.floor(seconds % 3600 / 60)
    return f"{hours} Hr" + (f" {minutes} Min" if minutes else "") if hours else f"{minutes} Min"


def calculate_tat(first: dict, evidence: list[dict]) -> dict:
    awbs = {r["awb"] for r in extract_awbs(first.get("body", ""))}

    def related(row: dict) -> bool:
        return row.get("group_id") == first.get("group_id") and any(
            r["awb"] in awbs for r in extract_awbs(row.get("body", ""))
        )

    sender = first.get("author") or first.get("author_name")
    response = next(
        (
            e
            for e in evidence
            if e["timestamp"] > first["timestamp"]
            and sender
            and (e.get("author") or e.get("author_name"))
            and (e.get("author") or e.get("author_name")) != sender
            and related(e)
            and e.get("response_verified") is True
            and e.get("responder_verified") is True
        ),
        None,
    )
    closure = next(
        (
            e
            for e in evidence
            if e["timestamp"] >= first["timestamp"] and related(e) and e.get("closure_verified") is True
        ),
        None,
    )
    return dict(
        responseSeconds=duration(first["timestamp"], response["timestamp"]) if response else None,
        resolutionSeconds=duration(first["timestamp"], closure["timestamp"]) if closure else None,
        responseAt=response["timestamp"] if response else None,
        closureAt=closure["timestamp"] if closure else None,
        responseMessageId=response["id"] if response else None,
        closureMessageId=closure["id"] if closure else None,
    )
