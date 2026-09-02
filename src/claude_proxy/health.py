"""Upstream token health probing.

A probe sends a minimal ``POST /v1/messages`` (10 tokens) so it also captures
fresh ``anthropic-ratelimit-*`` headers. Probes cost real subscription quota,
so we (a) skip tokens still inside a rate-limit cooldown and (b) only re-probe
on the configured cadence. Crucially, a 429 marks the token *rate_limited*
(recoverable) — not *unhealthy* (dead) — which is what a 401/403 means.
"""

from __future__ import annotations

import logging
import time

import httpx

from . import metrics, stores
from .stores import TokenStore

log = logging.getLogger("claude_proxy.health")

PROBE_MODEL = "claude-haiku-4-5"
PROBE_TIMEOUT = 30.0


def _retry_after(headers: httpx.Headers) -> float | None:
    """Shared with the request path — see ``stores.recovery_seconds``.

    The prober and the proxy must agree on how long a token stays sidelined,
    or one of them silently overrides the other's view of the pool.
    """
    return stores.recovery_seconds(headers)


def _set_health_gauge(store: TokenStore, name: str) -> None:
    metrics.TOKEN_HEALTHY.labels(token_name=name).set(
        1 if store.health[name].status in ("healthy", "unchecked") else 0
    )


async def probe(store: TokenStore, client: httpx.AsyncClient, name: str) -> bool:
    """Probe a single token, update its health, and return True if healthy."""
    try:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "authorization": f"Bearer {store.secret(name)}",
                "anthropic-beta": "oauth-2025-04-20",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": PROBE_MODEL,
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "Hi"}],
            },
            timeout=PROBE_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Health probe %s error: %s", name, e)
        store.mark_unhealthy(name)
        _set_health_gauge(store, name)
        return False

    store.record_headers(name, dict(resp.headers))
    metrics.update_util_gauges(name, dict(resp.headers))

    if 200 <= resp.status_code < 300:
        was = store.health[name].status
        store.mark_healthy(name)
        _set_health_gauge(store, name)
        if was not in ("healthy", "unchecked"):
            log.info("Token %s recovered (probe passed)", name)
        return True
    if resp.status_code == 429:
        store.mark_rate_limited(name, _retry_after(resp.headers))
        _set_health_gauge(store, name)
        log.warning("Health probe %s: 429 rate-limited", name)
        return False
    store.mark_unhealthy(name)
    _set_health_gauge(store, name)
    log.warning("Health probe %s: HTTP %d", name, resp.status_code)
    return False


async def health_loop(state) -> None:  # noqa: ANN001 - avoids import cycle
    """Periodically re-probe tokens that need it (ticks every 15s)."""
    import asyncio

    await asyncio.sleep(20)  # let startup settle
    while True:
        cfg = state.config
        now = time.time()
        for name in state.tokens.names():
            h = state.tokens.health[name]
            is_active = name == state.tokens.active
            interval = (
                cfg.active_probe_interval_seconds if is_active
                else cfg.health_probe_interval_seconds
            )
            # Don't probe a rate-limited token until its cooldown elapses.
            if h.status == "rate_limited" and now < h.rate_limited_until:
                continue
            # Healthy non-active tokens don't need re-probing — live traffic and
            # the active-token probe keep them fresh enough.
            if not is_active and h.status in ("healthy",):
                continue
            if now - h.last_checked >= interval:
                await probe(state.tokens, state.probe_client, name)
        await asyncio.sleep(15)
