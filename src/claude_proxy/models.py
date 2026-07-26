"""Pydantic models for configuration and validation."""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

from .budgets import DEFAULT_TIMEZONE
from .pricing import LITELLM_URL


class AutoRotation(BaseModel):
    """Automatic upstream-token rotation settings."""

    enabled: bool = False
    threshold_5h: float = 0.95
    target_max_util_5h: float = 0.50
    check_interval_seconds: int = 30
    probe_before_switch: bool = True
    cooldown_seconds: int = 120
    notify_only: bool = False

    @field_validator("threshold_5h", "target_max_util_5h")
    @classmethod
    def _ratio(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("must be between 0.0 and 1.0")
        return v

    @field_validator("check_interval_seconds")
    @classmethod
    def _interval(cls, v: int) -> int:
        if v < 5:
            raise ValueError("check_interval_seconds must be >= 5")
        return v

    @field_validator("cooldown_seconds")
    @classmethod
    def _cooldown(cls, v: int) -> int:
        if v < 0:
            raise ValueError("cooldown_seconds must be >= 0")
        return v


class Pricing(BaseModel):
    """Where per-token USD rates come from, and how often to re-fetch them."""

    online: bool = True
    source_url: str = LITELLM_URL
    refresh_hours: int = 12

    @field_validator("refresh_hours")
    @classmethod
    def _hours(cls, v: int) -> int:
        if not 1 <= v <= 24 * 30:
            raise ValueError("refresh_hours must be between 1 and 720")
        return v


class AppConfig(BaseModel):
    """Top-level, hot-reloadable proxy configuration (mirrors config.yaml)."""

    auto_rotation: AutoRotation = Field(default_factory=AutoRotation)
    health_probe_interval_seconds: int = 60
    active_probe_interval_seconds: int = 300
    upstream_timeout_seconds: int = 600
    # Calendar boundaries for the daily views and for every spend limit.
    timezone: str = DEFAULT_TIMEZONE
    pricing: Pricing = Field(default_factory=Pricing)
    # How long the hourly usage series is kept on disk (memory keeps ~40 days).
    usage_retention_days: int = 400

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as e:
            raise ValueError(f"unknown timezone {v!r} (use an IANA name, e.g. America/Toronto)") from e
        return v

    @field_validator("usage_retention_days")
    @classmethod
    def _retention(cls, v: int) -> int:
        if v < 40:
            raise ValueError("usage_retention_days must be >= 40 (the in-memory window)")
        return v

    @field_validator("health_probe_interval_seconds")
    @classmethod
    def _health(cls, v: int) -> int:
        if v < 10:
            raise ValueError("health_probe_interval_seconds must be >= 10")
        return v

    @field_validator("active_probe_interval_seconds")
    @classmethod
    def _active(cls, v: int) -> int:
        if v < 30:
            raise ValueError("active_probe_interval_seconds must be >= 30")
        return v

    @field_validator("upstream_timeout_seconds")
    @classmethod
    def _timeout(cls, v: int) -> int:
        if v < 10:
            raise ValueError("upstream_timeout_seconds must be >= 10")
        return v
