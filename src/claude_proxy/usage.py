"""Per-virtual-key, per-model usage tracking.

Thread-safe under an ``asyncio.Lock`` and persisted to SQLite with a debounced
background flusher (off the event loop) so the hot request path never blocks on
I/O. Tracks cache tokens in addition to input/output — with Claude's prompt
caching these dominate real spend, so ignoring them (as the original did) badly
undercounts usage.
"""

from __future__ import annotations

import asyncio
import copy
import logging

from . import db, metrics

log = logging.getLogger("claude_proxy.usage")

_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "requests",
)


class UsageTracker:
    def __init__(self, flush_interval: float = 5.0) -> None:
        self._stats: dict[str, dict[str, dict[str, int]]] = self._load()
        self._lock = asyncio.Lock()
        self._dirty = False
        self._flush_interval = flush_interval

    @staticmethod
    def _load() -> dict:
        try:
            return db.load_usage()
        except Exception:  # noqa: BLE001
            log.warning("usage table unreadable — starting fresh")
            return {}

    async def record(
        self,
        key_name: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read: int = 0,
        cache_creation: int = 0,
    ) -> None:
        total = input_tokens + output_tokens + cache_read + cache_creation
        if not key_name or total == 0:
            return
        model = model or "unknown"
        async with self._lock:
            key_stats = self._stats.setdefault(key_name, {})
            model_stats = key_stats.setdefault(model, {f: 0 for f in _FIELDS})
            for f in _FIELDS:
                model_stats.setdefault(f, 0)
            model_stats["input_tokens"] += input_tokens
            model_stats["output_tokens"] += output_tokens
            model_stats["cache_read_input_tokens"] += cache_read
            model_stats["cache_creation_input_tokens"] += cache_creation
            model_stats["requests"] += 1
            self._dirty = True

        metrics.INPUT_TOKENS.labels(key_name=key_name, model=model).inc(input_tokens)
        metrics.OUTPUT_TOKENS.labels(key_name=key_name, model=model).inc(output_tokens)
        metrics.CACHE_READ_TOKENS.labels(key_name=key_name, model=model).inc(cache_read)
        metrics.CACHE_CREATION_TOKENS.labels(key_name=key_name, model=model).inc(cache_creation)
        log.info(
            "USAGE key=%s model=%s in=%d out=%d cache_r=%d cache_w=%d",
            key_name, model, input_tokens, output_tokens, cache_read, cache_creation,
        )

    async def snapshot(self) -> dict:
        async with self._lock:
            return copy.deepcopy(self._stats)

    async def flush(self) -> None:
        async with self._lock:
            if not self._dirty:
                return
            snapshot = copy.deepcopy(self._stats)
            self._dirty = False
        try:
            await asyncio.to_thread(db.replace_usage, snapshot)
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to persist usage stats: %s", e)
            async with self._lock:
                self._dirty = True  # retry next tick

    async def flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            await self.flush()
