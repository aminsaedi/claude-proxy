"""Spend limits: calendar windows in the operator's timezone, and the store
that holds each virtual key's caps.

A key may carry several caps at once — ``$3/hour`` *and* ``$10/day`` is the
motivating example — and every one of them is checked on every request. Windows
are **calendar-aligned** in the configured timezone rather than rolling, so a
cap resets at a time the operator can predict: on the hour, at local midnight,
at Monday midnight, on the 1st.

Hourly usage buckets are stored in UTC, which lines up exactly with local
window boundaries for every whole-hour timezone (America/Toronto included).
Zones offset by :30/:45 (Asia/Kolkata, Asia/Kathmandu) land mid-bucket, so
their boundaries round to the nearest hour — noted here rather than solved,
since sub-hour buckets would cost far more than that precision is worth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db

log = logging.getLogger("claude_proxy.budgets")

PERIODS = ("hour", "day", "week", "month")
PERIOD_LABELS = {"hour": "per hour", "day": "per day", "week": "per week", "month": "per month"}
DEFAULT_TIMEZONE = "America/Toronto"


def zone(name: str) -> ZoneInfo:
    """Resolve a timezone name, falling back to the default and then to UTC."""
    for candidate in (name, DEFAULT_TIMEZONE, "UTC"):
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            log.warning("Unknown timezone %r — falling back", candidate)
    return ZoneInfo("UTC")  # pragma: no cover - UTC is always present


def window_bounds(period: str, now: float, tz: ZoneInfo) -> tuple[float, float]:
    """(start, end) epoch seconds of the calendar window containing ``now``.

    ``end`` is when the window rolls over, i.e. when the cap frees up again.
    """
    local = datetime.fromtimestamp(now, tz)
    if period == "hour":
        start = local.replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
    elif period == "day":
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif period == "week":  # ISO weeks: Monday 00:00 local
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        start = midnight - timedelta(days=midnight.weekday())
        end = start + timedelta(days=7)
    elif period == "month":
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (start + timedelta(days=32)).replace(day=1)
    else:
        raise ValueError(f"unknown period: {period!r}")
    # ZoneInfo arithmetic is wall-clock arithmetic and ``timestamp()`` resolves
    # the offset at each local time, so a DST shift inside the window still
    # yields the right instants for its edges (a 23h or 25h "day").
    return start.timestamp(), end.timestamp()


def day_start(now: float, tz: ZoneInfo, days_ago: int = 0) -> float:
    """Epoch seconds of local midnight, ``days_ago`` days before today."""
    local = datetime.fromtimestamp(now, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return (local - timedelta(days=days_ago)).timestamp()


@dataclass
class LimitStatus:
    """One cap, evaluated against the current window."""

    period: str
    limit_usd: float
    spent_usd: float
    window_start: float
    resets_at: float

    @property
    def over(self) -> bool:
        return self.spent_usd >= self.limit_usd > 0

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)

    @property
    def ratio(self) -> float:
        return self.spent_usd / self.limit_usd if self.limit_usd > 0 else 0.0

    def payload(self) -> dict:
        return {
            "period": self.period,
            "limit_usd": self.limit_usd,
            "spent_usd": self.spent_usd,
            "remaining_usd": self.remaining_usd,
            "ratio": self.ratio,
            "over": self.over,
            "window_start": self.window_start,
            "resets_at": self.resets_at,
        }

    def message(self) -> str:
        return (
            f"Spend limit reached: ${self.spent_usd:.2f} of ${self.limit_usd:.2f} "
            f"{PERIOD_LABELS.get(self.period, self.period)}."
        )


def evaluate(
    key_name: str,
    limits: dict[str, float],
    spend_between,  # noqa: ANN001 - (key, start, end) -> cost in USD
    now: float,
    tz: ZoneInfo,
) -> list[LimitStatus]:
    """Score every cap on a key, tightest (closest to its limit) first."""
    out = []
    for period in PERIODS:
        cap = limits.get(period)
        if not cap or cap <= 0:
            continue
        start, end = window_bounds(period, now, tz)
        out.append(LimitStatus(period, float(cap), spend_between(key_name, start, end), start, end))
    out.sort(key=lambda s: s.ratio, reverse=True)
    return out


class LimitStore:
    """Per-key spend caps, cached in memory and refreshed from the DB.

    Mirrors :class:`~claude_proxy.stores.VirtualKeyStore`: the request path only
    ever reads the cache, and a background loop picks up changes made by
    ``manage.py`` or another process within a few seconds.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, dict[str, float]] = {}
        self._sig: frozenset = frozenset()
        self.load()

    def load(self) -> None:
        self._by_key = db.list_key_limits()
        self._sig = self._signature(self._by_key)

    @staticmethod
    def _signature(by_key: dict[str, dict[str, float]]) -> frozenset:
        return frozenset(
            (name, period, value)
            for name, limits in by_key.items()
            for period, value in limits.items()
        )

    def reload_if_changed(self) -> bool:
        try:
            by_key = db.list_key_limits()
        except Exception as e:  # noqa: BLE001 - keep enforcing the cached caps
            log.warning("Failed to reload spend limits: %s", e)
            return False
        sig = self._signature(by_key)
        if sig == self._sig:
            return False
        self._by_key, self._sig = by_key, sig
        log.info("Reloaded spend limits from DB (%d keys capped)", len(by_key))
        return True

    def for_key(self, name: str) -> dict[str, float]:
        return dict(self._by_key.get(name, {}))

    def all(self) -> dict[str, dict[str, float]]:
        return {k: dict(v) for k, v in self._by_key.items()}

    def set(self, name: str, limits: dict[str, float]) -> None:
        """Persist and cache a key's full set of caps (empty dict = uncapped)."""
        clean = {p: float(v) for p, v in limits.items() if p in PERIODS and float(v) > 0}
        db.set_key_limits(name, clean)
        if clean:
            self._by_key[name] = clean
        else:
            self._by_key.pop(name, None)
        self._sig = self._signature(self._by_key)


def parse_limits(raw: dict) -> dict[str, float]:
    """Validate a ``{"hour": 3, "day": "10"}`` payload from the admin API.

    Blank / null / zero entries mean "no cap for this period" and are dropped.
    """
    out: dict[str, float] = {}
    for period, value in (raw or {}).items():
        if period not in PERIODS:
            raise ValueError(f"unknown period {period!r} (expected one of {', '.join(PERIODS)})")
        if value is None or value == "":
            continue
        try:
            amount = float(value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{period}: {value!r} is not a number") from e
        if amount < 0:
            raise ValueError(f"{period}: limit must be positive")
        if amount > 0:
            out[period] = amount
    return out
