"""Module settings; imports never connect or change AI Box settings."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path


def env(name: str, default: str = "") -> str:
    return os.getenv(name, "").strip() or default


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: env("ESCALATION_DATABASE_URL", ""))
    sqlite_path: str = field(
        default_factory=lambda: env(
            "ESCALATION_SQLITE_PATH", str(Path(__file__).resolve().parents[2] / "data" / "escalation.sqlite3")
        )
    )
    sidecar_url: str = field(
        default_factory=lambda: env("WHATSAPP_SIDECAR_URL", "http://127.0.0.1:3101").rstrip("/")
    )
    sidecar_secret: str = field(default_factory=lambda: env("WHATSAPP_SIDECAR_SECRET", ""), repr=False)
    api_key: str = field(
        default_factory=lambda: env(
            "ESCALATION_DEEPSEEK_API_KEY", env("DEEPSEEK_API_KEY", env("Ai_box_key", ""))
        ),
        repr=False,
    )
    model: str = field(default_factory=lambda: env("ESCALATION_DEEPSEEK_MODEL", "deepseek-v4-pro"))
    base_url: str = field(
        default_factory=lambda: env("ESCALATION_DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    )
    max_tokens: int = field(default_factory=lambda: int(env("ESCALATION_DEEPSEEK_MAX_TOKENS", "80000")))
    reasoning: str = field(default_factory=lambda: env("ESCALATION_DEEPSEEK_REASONING", "max"))
    recipient: str = field(default_factory=lambda: env("RECIPIENT_NUMBER", ""))
    gemini_key: str = field(default_factory=lambda: env("GEMINI_API_KEY", ""), repr=False)
    gemini_model: str = field(default_factory=lambda: env("GEMINI_MEDIA_MODEL", "gemini-3.1-flash-lite"))
    concurrency: int = 4
    retention_days: int = field(default_factory=lambda: int(env("ESCALATION_RETENTION_DAYS", "30")))
    scheduler_enabled: bool = field(
        default_factory=lambda: env("ESCALATION_SCHEDULER_ENABLED", "false").lower() == "true"
    )
    price_input: float = 0.435
    price_output: float = 0.87
    usd_inr: float = 94
