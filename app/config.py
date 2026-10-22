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
    # Plan-first reports use one typed planning call and one grounded narrative call.
    report_ai_planner: bool = True
    deepseek_input_cost_per_million: float | None = None
    deepseek_output_cost_per_million: float | None = None
    # Local development only: the user ID for AI Box requests that send no X-User-Id.
    # Empty (default) rejects such requests, so reports never fall into a shared account.
    dev_user_id: str = ""
    # Stop when the supplied shipments are not the complete, in-scope set the source matched.
    require_complete_source: bool = True
    # Audit-only by default: numerical accuracy failures are logged but do not block delivery.
    report_accuracy_strict: bool = False
    # Whole /generate request (shipment fetch + analysis + DeepSeek); exceeded -> 504.
    report_deadline_seconds: float = 170.0

    # Backend-owned shipment fetch (source="shipment_api"). The URL is fixed by configuration;
    # requests never choose the host. Empty base URL disables the mode (503).
    shipment_api_base_url: str = ""
    shipment_api_path: str = "/api/get-order-list"
    shipment_api_connect_timeout: float = 5.0
    shipment_api_read_timeout: float = 30.0
    shipment_api_total_timeout: float = 60.0      # every upstream call of one report together
    shipment_api_retries: int = 1                 # per call, for timeouts, network errors and 5xx only
    shipment_api_fetch_attempts: int = 2          # whole re-reads after the source changed mid-fetch
    shipment_api_max_records: int = 5000
    shipment_api_max_page_size: int = 5000        # largest perPage ever requested
    shipment_api_max_requests: int = 30           # upstream calls per fetch attempt
    shipment_api_max_response_bytes: int = 64 * 1024 * 1024
    shipment_api_count_page_size: int = 1
    shipment_api_segment_type: str = "domestic"       # default segment for backend-owned shipment requests
    # Selectable date fields, each sent verbatim as date_filter_type. Only documented values belong here:
    # the API's default date field (no date_filter_type) is undocumented, so it is not offered.
    shipment_api_date_fields: tuple[str, ...] = ("manifest_datetime",)
    shipment_api_max_range_days: int = 186
    # Triggers config reload

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
            report_ai_planner=_bool("REPORT_AI_PLANNER", True),
            deepseek_input_cost_per_million=_float("DEEPSEEK_INPUT_COST_PER_MILLION", 0) if _str("DEEPSEEK_INPUT_COST_PER_MILLION") else None,
            deepseek_output_cost_per_million=_float("DEEPSEEK_OUTPUT_COST_PER_MILLION", 0) if _str("DEEPSEEK_OUTPUT_COST_PER_MILLION") else None,
            dev_user_id=_str("AI_BOX_DEV_USER_ID"),
            require_complete_source=_bool("REPORT_REQUIRE_COMPLETE_SOURCE", True),
            report_accuracy_strict=_bool("REPORT_ACCURACY_STRICT", False),
            report_deadline_seconds=_float("REPORT_DEADLINE_SECONDS", 170.0, minimum=5.0),
            shipment_api_base_url=_str("SHIPMENT_API_BASE_URL").rstrip("/"),
            shipment_api_connect_timeout=_float("SHIPMENT_API_CONNECT_TIMEOUT", 5.0, minimum=0.5),
            shipment_api_read_timeout=_float("SHIPMENT_API_READ_TIMEOUT", 30.0, minimum=1.0),
            shipment_api_total_timeout=_float("SHIPMENT_API_TOTAL_TIMEOUT", 60.0, minimum=1.0),
            shipment_api_retries=_int("SHIPMENT_API_RETRIES", 1),
            shipment_api_fetch_attempts=_int("SHIPMENT_API_FETCH_ATTEMPTS", 2, minimum=1),
            shipment_api_max_records=_int("SHIPMENT_API_MAX_RECORDS", 5000, minimum=1),
            shipment_api_max_page_size=_int("SHIPMENT_API_MAX_PAGE_SIZE", 5000, minimum=1),
            shipment_api_max_requests=_int("SHIPMENT_API_MAX_REQUESTS", 30, minimum=2),
            shipment_api_max_response_bytes=_int("SHIPMENT_API_MAX_RESPONSE_BYTES", 64 * 1024 * 1024, minimum=1024),
            shipment_api_count_page_size=_int("SHIPMENT_API_COUNT_PAGE_SIZE", 1, minimum=1),
            shipment_api_segment_type="domestic",
            shipment_api_date_fields=_list("SHIPMENT_API_DATE_FIELDS", ("manifest_datetime",)),
            shipment_api_max_range_days=_int("SHIPMENT_API_MAX_RANGE_DAYS", 186, minimum=1),
        )


settings = Settings.from_env()
