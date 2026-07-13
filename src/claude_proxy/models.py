"""Pydantic models for configuration and validation."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


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


class AppConfig(BaseModel):
    """Top-level, hot-reloadable proxy configuration (mirrors config.yaml)."""

    auto_rotation: AutoRotation = Field(default_factory=AutoRotation)
    health_probe_interval_seconds: int = 60
    active_probe_interval_seconds: int = 300
    upstream_timeout_seconds: int = 600

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
