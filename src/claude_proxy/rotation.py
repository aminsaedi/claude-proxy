"""Health-aware automatic token rotation.

Rotates the active token when it becomes *unusable* (dead or rate-limited) OR
when its 5h utilization crosses the threshold. The key fix over the original:
when the active token is unusable, fail over to the least-utilized healthy
token **regardless** of ``target_max_util_5h`` — a rate-limited active token
must never strand traffic just because the alternative is above the target.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import metrics
from .health import probe

log = logging.getLogger("claude_proxy.rotation")


async def _find_candidate(state, require_under_target: bool) -> str | None:  # noqa: ANN001
    cfg = state.config.auto_rotation
    store = state.tokens
    now = time.time()
    for name in store.failover_order(now):
        if name == store.active:
            continue
        if not store.health[name].usable(now):
            continue
        if cfg.probe_before_switch:
            if not await probe(store, state.probe_client, name):
                continue
        util = store.health[name].util_5h()
        if require_under_target:
            if util is not None and util < cfg.target_max_util_5h:
                return name
            if util is None and cfg.probe_before_switch:
                return name  # freshly probed, no util data yet
            continue
        return name  # any usable token will do (active is unusable)
    return None


def _record(state, event: dict) -> None:  # noqa: ANN001
    state.rotation_log.append(event)
    if len(state.rotation_log) > 50:
        del state.rotation_log[:-50]


async def rotation_loop(state) -> None:  # noqa: ANN001
    await asyncio.sleep(25)  # let startup + first probes settle
    while True:
        cfg = state.config.auto_rotation
        interval = max(state.config.auto_rotation.check_interval_seconds, 5)
        if cfg.enabled:
            await _maybe_rotate(state)
        await asyncio.sleep(interval)


async def _maybe_rotate(state) -> None:  # noqa: ANN001
    cfg = state.config.auto_rotation
    store = state.tokens
    now = time.time()
    active_health = store.health[store.active]
    active_unusable = not active_health.usable(now)
    util = active_health.util_5h()
    high_util = util is not None and util >= cfg.threshold_5h

    if not active_unusable and not high_util:
        return

    # Dead/rate-limited active token bypasses the cooldown — we must move now.
    if not active_unusable and now - state.last_rotation_time < cfg.cooldown_seconds:
        return

    candidate = await _find_candidate(state, require_under_target=not active_unusable)
    reason = "active_unusable" if active_unusable else "high_util"
    if not candidate:
        log.warning(
            "AUTO-ROTATE: %s but no suitable candidate (active=%s util=%s)",
            reason, store.active, f"{util * 100:.0f}%" if util is not None else "?",
        )
        return

    event = {
        "time": now,
        "from": store.active,
        "to": candidate,
        "reason": reason,
        "trigger_util_5h": util if util is not None else 0.0,
    }
    if cfg.notify_only:
        log.warning("AUTO-ROTATE (notify-only): would switch %s -> %s (%s)",
                    store.active, candidate, reason)
        event["action"] = "notify_only"
    else:
        old = store.active
        store.set_active(candidate)
        state.last_rotation_time = now
        metrics.AUTO_ROTATIONS.inc()
        log.warning("AUTO-ROTATE: switched %s -> %s (%s)", old, candidate, reason)
        event["action"] = "switched"
    _record(state, event)
