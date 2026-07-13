"""Wires the shared state and both FastAPI apps together and runs them."""

from __future__ import annotations

import asyncio
import logging
import os

import httpx
import uvicorn

from . import db, health, metrics, rotation
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
        self.usage = UsageTracker()
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


async def _vkey_reload_loop(state: AppState) -> None:
    while True:
        await asyncio.sleep(5)
        try:
            state.vkeys.reload_if_changed()
        except Exception as e:  # noqa: BLE001
            log.warning("vkey reload check failed: %s", e)


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
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await state.usage.flush()
        if state.client is not None:
            await state.client.aclose()
