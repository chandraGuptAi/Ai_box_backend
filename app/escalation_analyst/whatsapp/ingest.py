"""Idempotent message capture and optional asynchronous media enrichment."""

from __future__ import annotations
from .media import understand


def ingest(runtime, data: dict) -> dict:
    result = runtime.groups.ingest_info(data)
    if result["inserted"] and data.get("media"):
        caption = data.get("body") or ""
        runtime.groups.update_body(result["id"], caption + " [media analysing]")

        def enrich():
            text = understand(data["media"], data["msg_type"], runtime.settings)
            runtime.groups.update_body(result["id"], (caption + " " + text).strip())

        try:
            runtime.executor.submit(enrich)
        except RuntimeError:
            runtime.groups.update_body(result["id"], caption + " [media: analysis unavailable]")
    return result
