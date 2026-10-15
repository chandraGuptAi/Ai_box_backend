"""Application settings loaded from environment variables and the local .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Variables already set in the process environment take precedence over .env.
load_dotenv(BASE_DIR / ".env", override=False)

_PLACEHOLDER_KEYS = {"", "your_deepseek_api_key", "changeme"}


def _str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _int(name: str, default: int, minimum: int = 0) -> int:
    raw = _str(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer, got {raw!r}.") from exc
    if value < minimum:
        raise ValueError(f"Environment variable {name} must be >= {minimum}, got {value}.")
    return value


def _float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = _str(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be a number, got {raw!r}.") from exc
    if value < minimum:
        raise ValueError(f"Environment variable {name} must be >= {minimum}, got {value}.")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _str(name)
    if not raw:
        return default
    return tuple(item.strip().rstrip("/") for item in raw.split(",") if item.strip())


def _api_key() -> str:
    # "Ai_box_key" is the documented name; the others are accepted as fallbacks
    # because environment variable names are case-sensitive outside Windows.
    for name in ("Ai_box_key", "AI_BOX_KEY", "DEEPSEEK_API_KEY"):
        value = _str(name)
        if value:
            return value
    return ""


@dataclass(frozen=True)
class Settings:
    app_name: str
    app_version: str
    environment: str
    log_level: str
    enable_docs: bool

    deepseek_api_key: str = field(repr=False)
    deepseek_model: str
    deepseek_base_url: str
    deepseek_timeout_seconds: float
    deepseek_max_retries: int
    deepseek_max_output_tokens: int
    deepseek_temperature: float
    # When the AI narrative's numbers cannot be verified, return the code-written narrative
    # (numbers from Python-calculated insights) instead of HTTP 502.
    deterministic_fallback: bool

    cors_allowed_origins: tuple[str, ...]

    max_request_bytes: int
    max_records: int
    max_prompt_chars: int
    ai_sample_records: int

    save_generated_files: bool
    output_dir: Path
    pdf_font_regular: str
    pdf_font_bold: str
    deepseek_compact_output_tokens: int = 1600
    deepseek_input_cost_per_million: float | None = None
    deepseek_output_cost_per_million: float | None = None

    @property
    def deepseek_configured(self) -> bool:
        return self.deepseek_api_key not in _PLACEHOLDER_KEYS

    @classmethod
    def from_env(cls) -> Settings:
        output_dir = Path(_str("OUTPUT_DIR", "output"))
        if not output_dir.is_absolute():
            output_dir = BASE_DIR / output_dir

        return cls(
            app_name=_str("APP_NAME", "AI Box Report Backend"),
            app_version=_str("APP_VERSION", "1.0.0"),
            environment=_str("APP_ENV", "development"),
            log_level=_str("LOG_LEVEL", "INFO").upper(),
            enable_docs=_bool("ENABLE_DOCS", True),
            deepseek_api_key=_api_key(),
            deepseek_model=_str("DEEPSEEK_MODEL", "deepseek-chat"),
            deepseek_base_url=_str("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            deepseek_timeout_seconds=_float("DEEPSEEK_TIMEOUT_SECONDS", 120.0, minimum=1.0),
            deepseek_max_retries=_int("DEEPSEEK_MAX_RETRIES", 0),
            deepseek_max_output_tokens=_int("DEEPSEEK_MAX_OUTPUT_TOKENS", 4096, minimum=256),
            deepseek_temperature=_float("DEEPSEEK_TEMPERATURE", 0.2),
            deterministic_fallback=_bool("REPORT_DETERMINISTIC_FALLBACK", True),
            cors_allowed_origins=_list("CORS_ALLOWED_ORIGINS", ("http://localhost:4200",)),
            max_request_bytes=_int("MAX_REQUEST_BYTES", 10 * 1024 * 1024, minimum=1024),
            max_records=_int("MAX_RECORDS", 20000, minimum=1),
            max_prompt_chars=_int("MAX_PROMPT_CHARS", 2000, minimum=50),
            ai_sample_records=_int("AI_SAMPLE_RECORDS", 25),
            save_generated_files=_bool("SAVE_GENERATED_FILES", False),
            output_dir=output_dir,
            pdf_font_regular=_str("PDF_FONT_REGULAR"),
            pdf_font_bold=_str("PDF_FONT_BOLD"),
            deepseek_compact_output_tokens=_int("DEEPSEEK_COMPACT_OUTPUT_TOKENS", 1600, minimum=512),
            deepseek_input_cost_per_million=_float("DEEPSEEK_INPUT_COST_PER_MILLION", 0) if _str("DEEPSEEK_INPUT_COST_PER_MILLION") else None,
            deepseek_output_cost_per_million=_float("DEEPSEEK_OUTPUT_COST_PER_MILLION", 0) if _str("DEEPSEEK_OUTPUT_COST_PER_MILLION") else None,
        )


settings = Settings.from_env()
