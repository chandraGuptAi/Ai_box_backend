"""Escalation-specific streaming client; never modifies the AI Box client or prompts."""

from __future__ import annotations

import asyncio
import logging
import time
from openai import AsyncOpenAI
from ..errors import EscalationError

logger = logging.getLogger("escalation_analyst.ai")


class Provider:
    def __init__(self, settings, reports):
        self.settings, self.reports = settings, reports

    async def _stream(self, system: str, user: str) -> tuple[str, object]:
        async with AsyncOpenAI(
            api_key=self.settings.api_key, base_url=self.settings.base_url, max_retries=0, timeout=300
        ) as client:
            # Bound initial thinking, each subsequent chunk, and the entire request separately.
            stream = await asyncio.wait_for(
                client.chat.completions.create(
                    model=self.settings.model,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    stream=True,
                    stream_options={"include_usage": True},
                    reasoning_effort=self.settings.reasoning,
                    max_tokens=self.settings.max_tokens,
                ),
                300,
            )
            try:
                pieces, usage = [], None
                iterator = stream.__aiter__()
                first = True
                while True:
                    try:
                        chunk = await asyncio.wait_for(anext(iterator), 300 if first else 120)
                    except StopAsyncIteration:
                        break
                    first = False
                    if chunk.usage:
                        usage = chunk.usage
                    for choice in chunk.choices:
                        if choice.delta.content:
                            pieces.append(choice.delta.content)
                        if choice.finish_reason == "length":
                            logger.warning("Escalation output reached the configured token limit")
                return "".join(pieces), usage
            finally:
                await stream.close()

    def generate(self, system: str, user: str, meta: dict) -> str:
        if not self.settings.api_key or self.settings.api_key.startswith("your_"):
            raise EscalationError(503, "Escalation AI key is not configured")
        error = None
        for attempt in range(3):
            try:

                async def bounded():
                    return await asyncio.wait_for(self._stream(system, user), 1500)

                text, usage = asyncio.run(bounded())
                if not text.strip():
                    raise ValueError("Empty AI response")
                if usage:
                    usd = (
                        usage.prompt_tokens / 1e6 * self.settings.price_input
                        + usage.completion_tokens / 1e6 * self.settings.price_output
                    )
                    self.reports.usage(
                        dict(
                            **meta,
                            model=self.settings.model,
                            input_tokens=usage.prompt_tokens,
                            output_tokens=usage.completion_tokens,
                            cost_usd=usd,
                            cost_inr=usd * self.settings.usd_inr,
                        )
                    )
                return text
            except Exception as exc:
                error = exc
                logger.warning("Escalation generation attempt %s failed: %s", attempt + 1, type(exc).__name__)
                if attempt < 2:
                    time.sleep((attempt + 1) * 2)
        raise EscalationError(502, "Escalation AI generation failed; retry the analysis.") from error
