"""Pydantic models for configuration and validation."""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

from .audit import MODES as AUDIT_MODES
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


class Audit(BaseModel):
    """Request + prompt audit log (see ``audit.py``).

    ``mode`` is the privacy dial: ``off`` records nothing, ``meta`` records one
    row per request without any prompt text, ``full`` also stores the prompt and
    the completion. Retention is enforced on both axes — whichever bites first.
    """

    mode: str = "full"
    retention_days: int = 7
    max_gb: float = 2.0
    # Per-body cap. Claude Code sends very large prompts; storing every byte of
    # a 2MB context would burn the size budget on a handful of requests, and
    # the head of a prompt is what identifies it.
    max_body_kb: int = 256

    @field_validator("mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        if v not in AUDIT_MODES:
            raise ValueError(f"mode must be one of {', '.join(AUDIT_MODES)}")
        return v

    @field_validator("retention_days")
    @classmethod
    def _days(cls, v: int) -> int:
        if not 1 <= v <= 365:
            raise ValueError("retention_days must be between 1 and 365")
        return v

    @field_validator("max_gb")
    @classmethod
    def _gb(cls, v: float) -> float:
        if not 0.05 <= v <= 512:
            raise ValueError("max_gb must be between 0.05 and 512")
        return v

    @field_validator("max_body_kb")
    @classmethod
    def _body(cls, v: int) -> int:
        if not 1 <= v <= 8192:
            raise ValueError("max_body_kb must be between 1 and 8192")
        return v

    @property
    def max_bytes(self) -> int:
        return int(self.max_gb * 1024**3)

    @property
    def max_body_bytes(self) -> int:
        return self.max_body_kb * 1024


class AuthGuard(BaseModel):
    """Throttle for repeated invalid-key attempts, per caller address.

    Consulted only after a key has already failed to resolve, so it can never
    delay an authenticated request. Set ``max_failures`` to 0 to disable.
    """

    max_failures: int = 20
    window_seconds: int = 300
    block_seconds: int = 300


class AppConfig(BaseModel):
    """Top-level, hot-reloadable proxy configuration.

    Stored as a single JSON row in the ``config`` table; ``config.yaml`` is only
    the seed format read once by ``claude_proxy.migrate``.
    """

    auto_rotation: AutoRotation = Field(default_factory=AutoRotation)
    health_probe_interval_seconds: int = 60
    active_probe_interval_seconds: int = 300
    upstream_timeout_seconds: int = 600
    # SSE keepalive for `stream: true` requests, in seconds; 0 disables it.
    #
    # Cloudflare's proxy read timeout (measured: 125s on this zone, adjustable
    # only on Enterprise) is a deadline on *time to first byte*, not on total
    # duration — a response that has started streaming survives indefinitely
    # (measured: 304s). Large-context requests can leave Anthropic silent for
    # well over a minute before the first token, which puts them within reach
    # of that ceiling; when it fires the caller gets a 524 and the proxy never
    # even records the request. Setting this makes the proxy answer immediately
    # with SSE comment frames while it waits, so the edge clock never starts.
    #
    # The cost: the response is committed to `200 text/event-stream` before
    # upstream's status is known, so an upstream failure has to arrive as an
    # in-stream `error` event rather than an HTTP status.
    sse_keepalive_seconds: int = 0
    # How long a request may wait for the token pool to recover before the
    # error is handed to the caller. Failover only helps while *some* token is
    # healthy; this covers the window where none is, which upstream normally
    # clears in seconds. 0 restores the old behaviour of failing immediately.
    retry_budget_seconds: int = 60
    # Calendar boundaries for the daily views and for every spend limit.
    timezone: str = DEFAULT_TIMEZONE
    pricing: Pricing = Field(default_factory=Pricing)
    audit: Audit = Field(default_factory=Audit)
    auth_guard: AuthGuard = Field(default_factory=AuthGuard)
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

    @field_validator("sse_keepalive_seconds")
    @classmethod
    def _keepalive(cls, v: int) -> int:
        # The upper bound is what makes this useful: a gap longer than the
        # edge's read timeout would let the very deadline this exists to
        # defeat fire between two keepalives.
        if v and not (1 <= v <= 30):
            raise ValueError("sse_keepalive_seconds must be 0 (off) or between 1 and 30")
        return v

    @field_validator("retry_budget_seconds")
    @classmethod
    def _retry_budget(cls, v: int) -> int:
        # The ceiling is the upstream request timeout's neighbour: waiting
        # longer than a few minutes for capacity is indistinguishable from
        # hanging, and the honest answer at that point is the 429.
        if not (0 <= v <= 300):
            raise ValueError("retry_budget_seconds must be between 0 (off) and 300")
        return v
