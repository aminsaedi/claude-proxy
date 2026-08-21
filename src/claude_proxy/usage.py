"""Per-virtual-key usage tracking: lifetime totals, an hourly time series, and
the dollar cost of both.

Thread-safe under an ``asyncio.Lock`` and persisted to SQLite with a debounced
background flusher (off the event loop) so the hot request path never blocks on
I/O. Tracks cache tokens in addition to input/output — with Claude's prompt
caching these dominate real spend, so ignoring them (as the original did) badly
undercounts usage.

Two shapes are kept in memory:

* ``_stats``   — lifetime {key: {model: totals}}, mirrored to the ``usage`` table.
* ``_hourly``  — {key: {utc_hour: {model: totals}}}, mirrored to ``usage_hourly``.

The hourly series is what the 1d/3d/7d console views and the spend limits read.
Only the last ``memory_days`` of it is held in RAM (the DB keeps more); that
bound is what keeps a per-request limit check to a few hundred dict lookups.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import db, metrics

log = logging.getLogger("claude_proxy.usage")

_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "requests",
)
_COST = "cost_usd"
_ALL = (*_FIELDS, _COST)

HOUR = 3600
# How much of the time series to keep in RAM. Must comfortably exceed the
# longest limit window (a calendar month) and the longest console view (30d).
MEMORY_DAYS = 40


def hour_of(ts: float) -> int:
    """Floor an epoch timestamp to its UTC hour bucket."""
    return int(ts) // HOUR * HOUR


def _empty() -> dict[str, float]:
    return {f: 0 for f in _FIELDS} | {_COST: 0.0}


def _add(into: dict[str, float], src: dict[str, float]) -> None:
    for f in _ALL:
        into[f] += src.get(f, 0)


class UsageTracker:
    def __init__(self, pricing=None, flush_interval: float = 5.0, memory_days: int = MEMORY_DAYS) -> None:  # noqa: ANN001
        self.pricing = pricing
        self._memory_days = memory_days
        self._stats: dict[str, dict[str, dict[str, float]]] = self._load()
        self._hourly: dict[str, dict[int, dict[str, dict[str, float]]]] = self._load_hourly()
        # Un-persisted deltas, not absolute values. See ``flush``.
        self._pending: dict[tuple[str, str], dict[str, float]] = {}
        self._pending_hourly: dict[tuple[int, str, str], dict[str, float]] = {}
        self._lock = asyncio.Lock()
        self._flush_interval = flush_interval

    @staticmethod
    def _load() -> dict:
        try:
            return db.load_usage()
        except Exception:  # noqa: BLE001
            log.warning("usage table unreadable — starting fresh")
            return {}

    def _load_hourly(self) -> dict:
        out: dict[str, dict[int, dict[str, dict[str, float]]]] = {}
        try:
            rows = db.load_usage_hourly(since=hour_of(time.time()) - self._memory_days * 86400)
        except Exception:  # noqa: BLE001
            log.warning("usage_hourly table unreadable — starting the time series fresh")
            return out
        for r in rows:
            bucket = out.setdefault(r["key_name"], {}).setdefault(r["hour_start"], {})
            bucket[r["model"]] = {f: r[f] for f in _ALL}
        return out

    # --- ingest -----------------------------------------------------------

    async def record(
        self,
        key_name: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read: int = 0,
        cache_creation: int = 0,
        now: float | None = None,
    ) -> float:
        """Book one request's usage and return what it cost, in USD.

        The cost is returned rather than only recorded because the caller (the
        audit log) needs the same number, and pricing it twice would mean two
        table lookups for one request.
        """
        total = input_tokens + output_tokens + cache_read + cache_creation
        if not key_name or total == 0:
            return 0.0
        model = model or "unknown"
        now = time.time() if now is None else now
        hour = hour_of(now)
        cost = (
            self.pricing.cost(model, input_tokens, output_tokens, cache_read, cache_creation)
            if self.pricing is not None else 0.0
        )
        delta = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
            "requests": 1,
            _COST: cost,
        }
        async with self._lock:
            lifetime = self._stats.setdefault(key_name, {}).setdefault(model, _empty())
            for f in _ALL:
                lifetime.setdefault(f, 0)
            _add(lifetime, delta)

            bucket = self._hourly.setdefault(key_name, {}).setdefault(hour, {})
            _add(bucket.setdefault(model, _empty()), delta)

            # Accumulate what still has to reach the DB, separately from the
            # running totals, so a flush writes "+= this much" rather than
            # "= my whole view of the world".
            _add(self._pending.setdefault((key_name, model), _empty()), delta)
            _add(self._pending_hourly.setdefault((hour, key_name, model), _empty()), delta)

        metrics.INPUT_TOKENS.labels(key_name=key_name, model=model).inc(input_tokens)
        metrics.OUTPUT_TOKENS.labels(key_name=key_name, model=model).inc(output_tokens)
        metrics.CACHE_READ_TOKENS.labels(key_name=key_name, model=model).inc(cache_read)
        metrics.CACHE_CREATION_TOKENS.labels(key_name=key_name, model=model).inc(cache_creation)
        metrics.COST_USD.labels(key_name=key_name, model=model).inc(cost)
        log.info(
            "USAGE key=%s model=%s in=%d out=%d cache_r=%d cache_w=%d cost=$%.4f",
            key_name, model, input_tokens, output_tokens, cache_read, cache_creation, cost,
        )
        return cost

    # --- reads ------------------------------------------------------------
    #
    # These are plain synchronous methods that read the in-memory series
    # *without* taking the lock, which is what lets the request path check
    # spend limits with no await. That is safe because every mutation under the
    # lock is straight-line CPU work with no await inside it, so a reader can
    # never observe a half-applied update.

    def _buckets_between(self, key_name: str, start: float, end: float):
        """Yield the {model: totals} maps whose hour falls in ``[start, end)``.

        Two ways to find them, and which is cheaper depends on the window: walk
        the hours in range and look each one up, or walk the key's buckets and
        filter. A one-hour limit check does one lookup instead of scanning six
        weeks of history; a 30-day view scans instead of doing 720 lookups.
        """
        hours = self._hourly.get(key_name)
        if not hours:
            return
        start_h, end_i = hour_of(start), int(end)
        if end_i <= start_h:
            return
        # A bucket belongs to the window if it *starts* before `end`, so the
        # last one to visit is the hour containing end-1 — which keeps a
        # part-way-through hour (an "up to now" window) in the result.
        last_h = hour_of(end_i - 1)
        count = (last_h - start_h) // HOUR + 1
        if count < len(hours):
            for hour in range(start_h, last_h + HOUR, HOUR):
                models = hours.get(hour)
                if models:
                    yield models
        else:
            for hour, models in hours.items():
                if start_h <= hour < end_i:
                    yield models

    def totals_between(self, key_name: str, start: float, end: float) -> dict[str, float]:
        """Summed usage for one key over ``[start, end)`` (epoch seconds)."""
        out = _empty()
        for models in self._buckets_between(key_name, start, end):
            for m in models.values():
                _add(out, m)
        return out

    def spend_between(self, key_name: str, start: float, end: float) -> float:
        """Just the dollars — the hot path for limit checks."""
        return sum(
            m.get(_COST, 0.0)
            for models in self._buckets_between(key_name, start, end)
            for m in models.values()
        )

    def models_between(self, key_name: str, start: float, end: float) -> dict[str, dict[str, float]]:
        """Per-model breakdown for one key over a window."""
        out: dict[str, dict[str, float]] = {}
        for models in self._buckets_between(key_name, start, end):
            for name, m in models.items():
                _add(out.setdefault(name, _empty()), m)
        return out

    def daily_series(self, key_name: str, tz: ZoneInfo, days: int, now: float | None = None) -> list[dict]:
        """One entry per local calendar day, oldest first, today last.

        Days with no traffic are included as zeroes so the console can draw a
        continuous bar chart without filling gaps itself.
        """
        from .budgets import day_start  # imported here to avoid a cycle

        now = time.time() if now is None else now
        out = []
        for offset in range(days - 1, -1, -1):
            start = day_start(now, tz, offset)
            end = day_start(now, tz, offset - 1) if offset else now + 1
            totals = self.totals_between(key_name, start, end)
            out.append({
                "date": datetime.fromtimestamp(start, tz).strftime("%Y-%m-%d"),
                "start": start,
                **totals,
            })
        return out

    async def snapshot(self) -> dict:
        async with self._lock:
            return copy.deepcopy(self._stats)

    # --- persistence ------------------------------------------------------

    async def flush(self) -> None:
        """Push accumulated deltas to disk, then adopt what the DB now holds.

        Writing deltas is what makes a shared database file safe (see
        ``db.add_usage``). Reading the affected rows straight back is the other
        half of that: if a second process is also writing — the overlap window
        of a rolling deploy — this is how each one learns about the other's
        traffic instead of enforcing spend limits against a half-blind view.
        """
        async with self._lock:
            if not self._pending and not self._pending_hourly:
                return
            pending = self._pending
            pending_hourly = self._pending_hourly
            self._pending = {}
            self._pending_hourly = {}
        rows = [(k, m, *(d.get(f, 0) for f in _ALL)) for (k, m), d in pending.items()]
        hourly_rows = [
            (h, k, m, *(d.get(f, 0) for f in _ALL))
            for (h, k, m), d in pending_hourly.items()
        ]
        try:
            await asyncio.to_thread(db.add_usage, rows)
            await asyncio.to_thread(db.add_usage_hourly, hourly_rows)
            truth = await asyncio.to_thread(db.read_usage, list(pending))
            truth_hourly = await asyncio.to_thread(db.read_usage_hourly, list(pending_hourly))
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to persist usage stats: %s", e)
            async with self._lock:  # fold the deltas back in and retry next tick
                for lifetime_key, delta in pending.items():
                    _add(self._pending.setdefault(lifetime_key, _empty()), delta)
                for bucket_key, delta in pending_hourly.items():
                    _add(self._pending_hourly.setdefault(bucket_key, _empty()), delta)
            return
        async with self._lock:
            self._adopt(truth, truth_hourly)

    def _adopt(self, truth: dict, truth_hourly: dict) -> None:
        """Replace local totals with the DB's, re-adding anything since the read.

        A request can land between the write and the read-back; its delta is
        already in ``_pending`` again, so folding that in keeps the in-memory
        view a superset of the durable one rather than losing the straggler.
        """
        for (key_name, model), totals in truth.items():
            merged = dict(totals)
            _add(merged, self._pending.get((key_name, model), {}))
            self._stats.setdefault(key_name, {})[model] = merged
        for (hour, key_name, model), totals in truth_hourly.items():
            merged = dict(totals)
            _add(merged, self._pending_hourly.get((hour, key_name, model), {}))
            self._hourly.setdefault(key_name, {}).setdefault(hour, {})[model] = merged

    def prune_memory(self, now: float | None = None) -> None:
        """Drop in-RAM buckets past the retention horizon (the DB keeps more)."""
        cutoff = hour_of((time.time() if now is None else now) - self._memory_days * 86400)
        for key, hours in self._hourly.items():
            stale = [h for h in hours if h < cutoff]
            for h in stale:
                del hours[h]
            if stale:
                log.debug("Pruned %d stale hourly buckets for %s", len(stale), key)

    async def flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            await self.flush()
