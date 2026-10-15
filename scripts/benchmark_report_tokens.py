"""Compare the retained legacy prompt builder with the intent-aware builder.

Run from the project root: python -m scripts.benchmark_report_tokens [--live]
Only --live sends the repository's synthetic sample to the configured model.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
from pathlib import Path

from app.config import settings
from app.prompts.base import build_messages
from app.prompts.registry import get_report_definition
from app.prompts.report_intent import detect_intent, scope_analysis
from app.services.deepseek_service import DeepSeekService


async def benchmark(live=False):
    root = Path(__file__).resolve().parents[1]
    path = root / "samples" / "token_benchmark.json"
    result = json.loads(path.read_text(encoding="utf-8"))
    source = (root / "samples" / "shipment_request.json").read_bytes()
    request = json.loads(source)
    definition = get_report_definition("shipment")
    original = definition.analyze(request["data"])
    result["input_sha256"] = hashlib.sha256(source).hexdigest()
    for row in result["cases"].values():
        intent = detect_intent(row["prompt"])
        scoped = scope_analysis(original, request["data"], intent)
        before = build_messages(definition, original, row["prompt"])
        after = build_messages(definition, scoped, row["prompt"], intent)
        for label, messages in (("before", before), ("after", after)):
            size = 0 if label == "after" and intent.deterministic else sum(len(m["content"].encode("utf-8")) for m in messages)
            row[label + "_bytes"] = size
            row[label + "_estimated_tokens"] = math.ceil(size / 4)
        row["estimated_reduction_percent"] = round((1-row["after_bytes"]/row["before_bytes"])*100, 1)
    if live:
        events = []
        class Capture(logging.Handler):
            def emit(self, record):
                try:
                    event = json.loads(record.getMessage())
                except ValueError:
                    return
                if event.get("event") == "deepseek_usage":
                    events.append(event)
        audit = logging.getLogger("app.services.deepseek_service")
        handler = Capture()
        previous_level = audit.level
        audit.setLevel(logging.INFO)
        audit.addHandler(handler)
        service = DeepSeekService()
        row = result["cases"]["dashboard"]
        intent = detect_intent(row["prompt"])
        scoped = scope_analysis(original, request["data"], intent)
        measurement = {"measured_at": datetime.now(timezone.utc).isoformat(), "model": service.model,
                       "scope": "one synthetic dashboard request, same source records and user prompt; all attempts included"}
        try:
            for label, analysis, plan, limit in (("before", original, None, settings.deepseek_max_output_tokens),
                                                 ("after", scoped, intent, settings.deepseek_compact_output_tokens)):
                events.clear()
                try:
                    await service.generate_json(build_messages(definition, analysis, row["prompt"], plan), max_tokens=limit)
                    status = "valid_json"
                except Exception as exc:
                    status = getattr(exc, "code", type(exc).__name__)
                measurement[label] = {"status": status, "calls": len(events),
                    "prompt_tokens": sum(e["prompt_tokens"] for e in events),
                    "completion_tokens": sum(e["completion_tokens"] for e in events)}
                print(label, measurement[label], flush=True)
            result["live_dashboard"] = measurement
        finally:
            audit.removeHandler(handler)
            audit.setLevel(previous_level)
            if service._client:
                await service._client.close()
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    asyncio.run(benchmark(parser.parse_args().live))
