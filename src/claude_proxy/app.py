"""Wires the shared state and both FastAPI apps together and runs them."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from zoneinfo import ZoneInfo

import httpx
import uvicorn

from . import budgets, db, health, metrics, pricing, rotation
from .admin_app import admin_auth_enabled, build_admin_app
from .config import load_config
from .proxy_app import build_proxy_app
from .stores import TokenStore, VirtualKeyStore
from .usage import UsageTracker

log = logging.getLogger("claude_proxy")

UPSTREAM = "https://api.anthropic.com"


class AppState:
    """Container for all shared, mutable runtime state (no module globals)."""

    def __init__(self) -> None:
        db.init_schema()
        self.config = load_config()
        self.tokens = TokenStore()
        self.vkeys = VirtualKeyStore()
        self.pricing = pricing.PricingTable(url=self.config.pricing.source_url)
        self.usage = UsageTracker(pricing=self.pricing)
        self.limits = budgets.LimitStore()
        self.rotation_log: list[dict] = []
        self.last_rotation_time: float = 0.0
        self.client: httpx.AsyncClient | None = None
        self.probe_client: httpx.AsyncClient | None = None
        # Live push (SSE): each open /events stream registers a wakeup Event so a
        # mutation can nudge every viewer to re-send immediately instead of
        # waiting for its periodic recompute. asyncio.Event binds to the running
        # loop lazily, so constructing this outside a loop (e.g. tests) is fine.
        self._subscribers: set[asyncio.Event] = set()

    def subscribe(self) -> asyncio.Event:
        ev = asyncio.Event()
        self._subscribers.add(ev)
        return ev

    def unsubscribe(self, ev: asyncio.Event) -> None:
        self._subscribers.discard(ev)

    def notify(self) -> None:
        """Wake every live viewer so it re-checks state and pushes if changed."""
        for ev in self._subscribers:
            ev.set()

    # --- spend limits -----------------------------------------------------

    @property
    def tz(self) -> ZoneInfo:
        return budgets.zone(self.config.timezone)

    def limit_status(self, key_name: str, now: float | None = None) -> list[budgets.LimitStatus]:
        """Every cap on a key scored against its current window, tightest first."""
        caps = self.limits.for_key(key_name)
        if not caps:
            return []
        return budgets.evaluate(
            key_name, caps, self.usage.spend_between,
            time.time() if now is None else now, self.tz,
        )

    def limit_breach(self, key_name: str, now: float | None = None) -> budgets.LimitStatus | None:
        """The cap a request should be rejected under, or None if it may proceed.

        Called synchronously on the request path: it reads the in-memory hourly
        series, never the DB. Spend is booked *after* a response completes, so
        concurrent in-flight requests can overshoot a cap slightly — the cap is
        a budget guard, not a hard transactional ledger.
        """
        for status in self.limit_status(key_name, now):
            if status.over:
                return status
        return None


async def _vkey_reload_loop(state: AppState) -> None:
    """Pick up virtual-key and spend-limit edits made by manage.py or another pod."""
    while True:
        await asyncio.sleep(5)
        try:
            state.vkeys.reload_if_changed()
            state.limits.reload_if_changed()
        except Exception as e:  # noqa: BLE001
            log.warning("virtual key / limit reload check failed: %s", e)


async def _usage_retention_loop(state: AppState) -> None:
    """Trim the hourly series: RAM to ~40 days, disk to ``usage_retention_days``."""
    while True:
        try:
            state.usage.prune_memory()
            cutoff = time.time() - state.config.usage_retention_days * 86400
            removed = await asyncio.to_thread(db.prune_usage_hourly, int(cutoff))
            if removed:
                log.info("Pruned %d usage buckets older than %d days",
                         removed, state.config.usage_retention_days)
        except Exception as e:  # noqa: BLE001
            log.warning("usage retention pass failed: %s", e)
        await asyncio.sleep(6 * 3600)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    state = AppState()
    for name in state.tokens.names():
        metrics.TOKEN_HEALTHY.labels(token_name=name).set(1)

    if not admin_auth_enabled():
        log.warning(
            "ADMIN AUTH DISABLED — set ADMIN_PASSWORD to protect the admin UI. "
            "Relying on network isolation (Tailscale) alone."
        )

    timeout = httpx.Timeout(float(state.config.upstream_timeout_seconds))
    state.client = httpx.AsyncClient(base_url=UPSTREAM, timeout=timeout)
    state.probe_client = state.client

    proxy_app = build_proxy_app(state)
    admin_app = build_admin_app(state)
    proxy_port = int(os.environ.get("PROXY_PORT", "8080"))
    admin_port = int(os.environ.get("ADMIN_PORT", "8090"))

    proxy_srv = uvicorn.Server(uvicorn.Config(proxy_app, host="0.0.0.0", port=proxy_port, log_config=None))
    admin_srv = uvicorn.Server(uvicorn.Config(admin_app, host="0.0.0.0", port=admin_port, log_config=None))
    admin_srv.install_signal_handlers = lambda: None  # type: ignore[attr-defined]

    tasks = [
        asyncio.create_task(proxy_srv.serve()),
        asyncio.create_task(admin_srv.serve()),
        asyncio.create_task(state.usage.flush_loop()),
        asyncio.create_task(health.health_loop(state)),
        asyncio.create_task(rotation.rotation_loop(state)),
        asyncio.create_task(_vkey_reload_loop(state)),
        asyncio.create_task(pricing.refresh_loop(state)),
        asyncio.create_task(_usage_retention_loop(state)),
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await state.usage.flush()
        if state.client is not None:
            await state.client.aclose()
