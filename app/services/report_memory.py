"""Client of Ai_Box_Service report memory (``/api/report_memory/*``).

Before planning, a question that worked before returns its plan (no AI call) and similar successful
questions are shown to the planner as examples. After the report, the outcome is recorded; saves,
exports and likes/dislikes adjust it later. Memory never blocks a report: every failure is ignored.
Only the privacy-filtered question and the plan (field names and filter values) are sent.
"""
from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from typing import Any

import httpx
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger("ai_box.report_memory")
SERVICE_TOKEN_HEADER = "X-Ai-Box-Service-Token"
# The plan of the current report, recorded once the outcome is known.
_pending: ContextVar[dict[str, Any] | None] = ContextVar("report_memory_pending", default=None)


def _target() -> tuple[str, dict[str, str]] | None:
    base_url = os.getenv("AI_BOX_SERVICE_URL", "").strip().rstrip("/")
    if not base_url or os.getenv("REPORT_MEMORY", "true").strip().lower() in {"0", "false", "no", "off"}:
        return None
    headers = {}
    if token := os.getenv("AI_BOX_SERVICE_TOKEN", "").strip():
        headers[SERVICE_TOKEN_HEADER] = token
    return f"{base_url}/api/report_memory", headers


def scope_key(request, scope: dict[str, Any]) -> str:
    """Scope shape, not dates: the same question on other days reuses the same plan."""
    return "|".join(str(part) for part in (request.report_type, request.source, scope.get("status") or "all",
                                           scope.get("date_field") or "", "pins" if scope.get("pincodes") else ""))


async def _filtered(prompt: str) -> str | None:
    from app.security.privacy import guard
    try:
        return (await run_in_threadpool(guard.protect, [{"role": "user", "content": prompt}]))[0]["content"]
    except Exception:
        return None


async def _post(path: str, body: dict[str, Any], timeout: float = 3.0) -> dict[str, Any] | None:
    target = _target()
    if target is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            res = await client.post(target[0] + path, json=body, headers=target[1])
        res.raise_for_status()
        return res.json()
    except Exception as exc:
        logger.warning("Report memory %s skipped (%s).", path, type(exc).__name__)
        return None


async def lookup(prompt: str, key: str) -> tuple[str | None, dict[str, Any] | None]:
    """(filtered question, {"cached_plan", "examples"}) or (None, None) when memory is off or unavailable."""
    if _target() is None:
        return None, None
    filtered = await _filtered(prompt)
    if not filtered:
        return None, None
    return filtered, await _post("/lookup", {"prompt": filtered, "scope_key": key, "k": 3})


def remember(prompt: str | None, key: str, plan: dict[str, Any], cacheable: bool, reused: bool) -> None:
    _pending.set({"prompt": prompt, "scope_key": key, "plan": plan, "cacheable": cacheable, "reused": reused}
                 if prompt else None)


def reset() -> None:
    _pending.set(None)


async def record_outcome(report_id: str | None, outcome: str) -> None:
    pending = _pending.get()
    _pending.set(None)
    if pending:
        await _post("/record", {"prompt": pending["prompt"], "scope_key": pending["scope_key"],
                                "plan": pending["plan"], "report_id": report_id, "outcome": outcome,
                                "cacheable": pending["cacheable"]})


def send_feedback(report_id: str, signal: str) -> None:
    """Synchronous, for FastAPI background tasks after save/export/like responses."""
    target = _target()
    if target is None or not report_id:
        return
    try:
        with httpx.Client(timeout=3.0, trust_env=False) as client:
            client.post(target[0] + "/feedback", json={"report_id": report_id, "signal": signal},
                        headers=target[1]).raise_for_status()
    except Exception as exc:
        logger.warning("Report memory feedback skipped (%s).", type(exc).__name__)
