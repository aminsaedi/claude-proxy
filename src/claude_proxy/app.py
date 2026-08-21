"""Wires the shared state and both FastAPI apps together and runs them."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from zoneinfo import ZoneInfo

import httpx
import uvicorn

from . import audit as audit_mod
from . import budgets, db, health, metrics, pricing, rotation
from .admin_app import admin_auth_enabled, build_admin_app
from .config import load_config
from .proxy_app import build_proxy_app
from .stores import TokenStore, VirtualKeyStore
from .usage import UsageTracker

log = logging.getLogger("claude_proxy")

# Overridable so the proxy can be pointed at a stand-in during development
# without editing code; in every real deployment this is the default.
UPSTREAM = os.environ.get("CLAUDE_PROXY_UPSTREAM", "https://api.anthropic.com")

# Connection pool sizing. The default httpx ceiling (100 total / 20 keep-alive)
# is aimed at a client making a few calls, not at a proxy fronting every request
# a fleet of Claude Code sessions makes. Undersizing here shows up as queueing
# latency that looks like upstream slowness. Keep-alives are held long enough
# that a busy proxy essentially never pays for a TLS handshake.
POOL_LIMITS = httpx.Limits(
    max_connections=int(os.environ.get("PROXY_MAX_CONNECTIONS", "256")),
    max_keepalive_connections=int(os.environ.get("PROXY_MAX_KEEPALIVE", "128")),
    keepalive_expiry=120.0,
)

# How long to keep serving in-flight requests after SIGTERM. A streaming
# completion can legitimately run for minutes, and cutting one off is exactly
# the visible downtime a rolling deploy is supposed to avoid.
DRAIN_TIMEOUT = float(os.environ.get("PROXY_DRAIN_TIMEOUT", "90"))


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
        self.audit = audit_mod.AuditLog(
            audit_mod.default_path(),
            mode=self.config.audit.mode,
            retention_days=self.config.audit.retention_days,
            max_bytes=self.config.audit.max_bytes,
            max_body_bytes=self.config.audit.max_body_bytes,
        )
        self.rotation_log: list[dict] = []
        self.last_rotation_time: float = 0.0
        self.client: httpx.AsyncClient | None = None
        self.probe_client: httpx.AsyncClient | None = None
        self.started_at: float = time.time()
        # Last audit storage reading, refreshed off the request path by
        # ``_audit_metrics_loop``. The dashboard shows it on every frame, and
        # opening the audit DB per frame per viewer would be silly.
        self.audit_stats: dict = {}
        # Flipped by the shutdown handler. /healthz reports 503 while set, which
        # is what pulls this pod out of the ingress before it stops accepting.
        self.draining: bool = False
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

    def apply_audit_config(self) -> None:
        """Push the current config onto the running audit log.

        Mode and retention are hot-swappable — turning capture off, or dropping
        the size budget, takes effect on the next request and the next sweep
        without a restart.
        """
        cfg = self.config.audit
        self.audit.mode = cfg.mode
        self.audit.retention_days = cfg.retention_days
        self.audit.max_bytes = cfg.max_bytes
        self.audit.max_body_bytes = cfg.max_body_bytes
        if cfg.mode != "off":
            self.audit.start()

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


async def _audit_metrics_loop(state: AppState) -> None:
    """Publish audit queue/storage gauges without touching the request path."""
    while True:
        await asyncio.sleep(15)
        try:
            metrics.AUDIT_QUEUED.set(state.audit.depth())
            metrics.AUDIT_WRITTEN.set(state.audit.written)
            metrics.AUDIT_DROPPED.set(state.audit.dropped)
            if state.audit.enabled:
                stats = await asyncio.to_thread(state.audit.stats)
                state.audit_stats = stats
                metrics.AUDIT_BYTES.set(stats.get("bytes") or 0)
                metrics.AUDIT_ROWS.set(stats.get("rows") or 0)
                state.notify()
        except Exception as e:  # noqa: BLE001
            log.debug("audit metrics refresh failed: %s", e)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    state = AppState()
    for name in state.tokens.names():
        metrics.TOKEN_HEALTHY.labels(token_name=name).set(1)
    state.apply_audit_config()

    if not admin_auth_enabled():
        log.warning(
            "ADMIN AUTH DISABLED — set ADMIN_PASSWORD to protect the admin UI. "
            "Relying on network isolation (Tailscale) alone."
        )

    timeout = httpx.Timeout(float(state.config.upstream_timeout_seconds), connect=10.0)
    state.client = httpx.AsyncClient(base_url=UPSTREAM, timeout=timeout, limits=POOL_LIMITS)
    state.probe_client = state.client

    proxy_app = build_proxy_app(state)
    admin_app = build_admin_app(state)
    proxy_port = int(os.environ.get("PROXY_PORT", "8080"))
    admin_port = int(os.environ.get("ADMIN_PORT", "8090"))

    proxy_srv = uvicorn.Server(_uvicorn_config(proxy_app, proxy_port))
    admin_srv = uvicorn.Server(_uvicorn_config(admin_app, admin_port))
    _disarm_uvicorn_signals(proxy_srv)
    _disarm_uvicorn_signals(admin_srv)

    background = [
        asyncio.create_task(state.usage.flush_loop()),
        asyncio.create_task(health.health_loop(state)),
        asyncio.create_task(rotation.rotation_loop(state)),
        asyncio.create_task(_vkey_reload_loop(state)),
        asyncio.create_task(pricing.refresh_loop(state)),
        asyncio.create_task(_usage_retention_loop(state)),
        asyncio.create_task(_audit_metrics_loop(state)),
    ]
    servers = [
        asyncio.create_task(proxy_srv.serve()),
        asyncio.create_task(admin_srv.serve()),
    ]

    _install_shutdown(state, [proxy_srv, admin_srv])

    try:
        # Servers are what the process is *for*; a background loop dying is a
        # bug to log, not a reason to drop live traffic. Waiting on the servers
        # alone also means shutdown completes when they do.
        await asyncio.gather(*servers)
    finally:
        log.info("Shutting down: draining background tasks")
        for t in background:
            t.cancel()
        for t in background:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await state.usage.flush()
        state.audit.stop()
        if state.client is not None:
            await state.client.aclose()
        log.info("Shutdown complete")


def _uvicorn_config(app, port: int) -> uvicorn.Config:  # noqa: ANN001
    return uvicorn.Config(
        app, host="0.0.0.0", port=port, log_config=None,
        # Let in-flight streaming completions finish instead of being severed
        # the moment SIGTERM lands.
        timeout_graceful_shutdown=int(DRAIN_TIMEOUT),
        # uvloop + httptools when present: measurably less per-request overhead
        # than the asyncio/h11 defaults, and this process is nothing but I/O.
        loop="auto", http="auto",
    )


@contextlib.contextmanager
def _no_capture():
    yield


def _disarm_uvicorn_signals(server: uvicorn.Server) -> None:
    """Stop a uvicorn server from installing its own signal handlers.

    Two servers share this process, and uvicorn's handler only stops the server
    that installed it. Left alone, both call ``signal.signal`` during
    ``serve()``, the second registration wins, and a SIGTERM shuts down exactly
    one of them — the process then sits there holding the other open until the
    kubelet loses patience and sends SIGKILL, which severs every in-flight
    request. That is the opposite of a graceful drain, so shutdown is handled in
    one place (``_install_shutdown``) and both servers are disarmed here.

    Both spellings are covered because uvicorn changed the mechanism: modern
    versions wrap ``serve()`` in ``capture_signals()``, older ones called
    ``install_signal_handlers()``.
    """
    server.capture_signals = _no_capture  # type: ignore[method-assign]
    if hasattr(server, "install_signal_handlers"):
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]


def _install_shutdown(state: AppState, servers: list[uvicorn.Server],
                      lead: float | None = None) -> None:
    """Turn SIGTERM/SIGINT into: stop being ready, pause, then drain.

    The pause matters. Kubernetes sends SIGTERM and removes the pod from
    Endpoints at the same moment, and the ingress finds out about the removal
    asynchronously — so for a second or two after SIGTERM, new requests are
    still being routed here. Failing readiness immediately and only *then*
    winding the servers down means those in-flight arrivals are served by a pod
    that is still listening, instead of hitting a closed socket.
    """
    loop = asyncio.get_running_loop()
    if lead is None:
        lead = float(os.environ.get("PROXY_DRAIN_LEAD", "5"))
    triggered = False

    async def shutdown() -> None:
        nonlocal triggered
        if triggered:
            return
        triggered = True
        state.draining = True
        log.info("SIGTERM received — failing readiness, draining for %.0fs", lead)
        await asyncio.sleep(lead)
        for srv in servers:
            srv.should_exit = True

    def handler() -> None:
        loop.create_task(shutdown())  # noqa: RUF006 - fire and forget by design

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, handler)
