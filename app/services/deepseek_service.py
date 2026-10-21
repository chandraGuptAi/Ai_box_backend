"""All DeepSeek API access lives here (OpenAI-compatible SDK, JSON output mode)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx
import openai
from openai import AsyncOpenAI

from app.config import Settings, settings
from app.errors import (
    AIConfigurationError,
    AIResponseError,
    AIServiceError,
    AIServiceTimeoutError,
    AIServiceUnavailableError,
)

logger = logging.getLogger(__name__)

# Ordered reports make one typed planning call followed by one grounded narrative call.
# Rule-only legacy reports make one narrative call. No model-generated code is executed.


class DeepSeekService:
    def __init__(self, config: Settings = settings) -> None:
        self._settings = config
        self._client: AsyncOpenAI | None = None

    @property
    def model(self) -> str:
        return self._settings.deepseek_model

    def _get_client(self) -> AsyncOpenAI:
        if not self._settings.deepseek_configured:
            raise AIConfigurationError(
                "The AI service is not configured on the server. Set Ai_box_key in the backend .env file."
            )
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self._settings.deepseek_api_key,
                base_url=self._settings.deepseek_base_url,
                timeout=httpx.Timeout(self._settings.deepseek_timeout_seconds, connect=10.0),
                max_retries=self._settings.deepseek_max_retries,
            )
        return self._client

    async def generate_json(self, messages: list[dict[str, str]], *, max_tokens: int | None = None) -> dict[str, Any]:
        """Send chat messages in JSON output mode and return the parsed JSON object."""
        client = self._get_client()
        limit = min(max_tokens or self._settings.deepseek_max_output_tokens, self._settings.deepseek_max_output_tokens)
        for attempt in range(1, 3):
            try:
                content = await self._complete(client, messages, max_tokens=limit)
                break
            except AIResponseError as exc:
                if exc.code == "ai_response_truncated" and attempt < 2 and limit < self._settings.deepseek_max_output_tokens:
                    logger.warning("DeepSeek output was truncated at %s tokens; retrying once with full token budget %s.",
                                   limit, self._settings.deepseek_max_output_tokens)
                    limit = self._settings.deepseek_max_output_tokens
                    continue
                raise
        parsed = _parse_json_object(content)
        if parsed is None:
            logger.warning("DeepSeek returned empty or invalid JSON; no further call is made.")
            raise AIResponseError(
                "The AI service returned an incomplete response, so no report was created. Please try again.",
                code="ai_invalid_json", details={"ai_calls": 1},
            )
        return parsed

    async def _complete(self, client: AsyncOpenAI, messages: list[dict[str, str]], *, max_tokens: int | None = None) -> str:
        # httpx's read timeout is the gap between bytes, and DeepSeek sends blank keep-alive lines while a
        # non-streaming request waits, so it never fires. This is the whole call's deadline.
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(client.chat.completions.create(
                model=self._settings.deepseek_model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=self._settings.deepseek_temperature,
                max_tokens=max_tokens or self._settings.deepseek_max_output_tokens,
                stream=False,
            ), timeout=self._settings.deepseek_timeout_seconds)
        except asyncio.TimeoutError as exc:
            logger.warning("DeepSeek call exceeded the %.0fs deadline.", self._settings.deepseek_timeout_seconds)
            raise AIServiceTimeoutError("The AI service did not respond in time. Please try again.") from exc
        except openai.APITimeoutError as exc:
            raise AIServiceTimeoutError("The AI service did not respond in time. Please try again.") from exc
        except openai.AuthenticationError as exc:
            logger.error("DeepSeek rejected the API key (HTTP 401).")
            raise AIServiceError(
                "The AI service rejected the server's credentials.", code="ai_authentication_failed"
            ) from exc
        except openai.RateLimitError as exc:
            raise AIServiceUnavailableError(
                "The AI service is rate limiting requests. Please wait and try again.", code="ai_rate_limited"
            ) from exc
        except openai.APIConnectionError as exc:
            raise AIServiceUnavailableError("Could not connect to the AI service.") from exc
        except openai.APIStatusError as exc:
            logger.error("DeepSeek returned HTTP %s.", exc.status_code)
            if exc.status_code == 402:
                raise AIServiceError(
                    "The AI service account has insufficient balance.", code="ai_insufficient_balance"
                ) from exc
            if exc.status_code >= 500:
                raise AIServiceUnavailableError("The AI service is temporarily unavailable.") from exc
            raise AIServiceError(f"The AI service rejected the request (HTTP {exc.status_code}).") from exc
        except openai.OpenAIError as exc:
            logger.exception("Unexpected DeepSeek client error.")
            raise AIServiceError("Unexpected error while calling the AI service.") from exc

        usage = getattr(response, "usage", None)
        if usage is not None:
            input_price = self._settings.deepseek_input_cost_per_million
            output_price = self._settings.deepseek_output_cost_per_million
            estimated_cost = ((usage.prompt_tokens * input_price + usage.completion_tokens * output_price) / 1_000_000
                              if input_price is not None and output_price is not None else None)
            logger.info("%s", json.dumps({"event": "deepseek_usage", "model": self.model,
                "prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens,
                "max_output_tokens": max_tokens or self._settings.deepseek_max_output_tokens,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "estimated_cost": estimated_cost, "cost_basis": "configured rates; excludes cache discounts"}))

        if not response.choices:
            return ""
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise AIResponseError(
                "The AI response was too long and was cut off, so no report was created. "
                "Try a shorter or narrower request.",
                code="ai_response_truncated", details={"ai_calls": 1},
            )
        return choice.message.content or ""


def _parse_json_object(content: str) -> dict[str, Any] | None:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
