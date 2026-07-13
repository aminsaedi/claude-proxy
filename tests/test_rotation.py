from __future__ import annotations

from claude_proxy import rotation
from claude_proxy.app import AppState


def _state() -> AppState:
    s = AppState()
    s.config.auto_rotation.enabled = True
    s.config.auto_rotation.probe_before_switch = False
    s.config.auto_rotation.cooldown_seconds = 0
    s.config.auto_rotation.threshold_5h = 0.9
    s.config.auto_rotation.target_max_util_5h = 0.5
    s.last_rotation_time = 0.0
    return s


def _util(store, name, u):
    store.health[name].headers = {"anthropic-ratelimit-unified-5h-utilization": str(u)}


async def test_rotates_off_unusable_active_ignoring_target():
    s = _state()
    s.tokens.mark_rate_limited("a", 300)       # active is unusable
    s.tokens.mark_healthy("b")
    _util(s.tokens, "b", 0.95)                  # b is well ABOVE the target...
    s.tokens.active = "a"
    await rotation._maybe_rotate(s)
    assert s.tokens.active == "b"              # ...but we still fail over to it


async def test_high_util_rotates_to_candidate_under_target():
    s = _state()
    s.tokens.mark_healthy("a")
    s.tokens.mark_healthy("b")
    _util(s.tokens, "a", 0.95)                  # active over threshold
    _util(s.tokens, "b", 0.1)                   # candidate under target
    s.tokens.active = "a"
    await rotation._maybe_rotate(s)
    assert s.tokens.active == "b"


async def test_high_util_no_candidate_under_target_stays():
    s = _state()
    s.tokens.mark_healthy("a")
    s.tokens.mark_healthy("b")
    _util(s.tokens, "a", 0.95)
    _util(s.tokens, "b", 0.8)                   # above target — not eligible
    s.tokens.active = "a"
    await rotation._maybe_rotate(s)
    assert s.tokens.active == "a"


async def test_notify_only_does_not_switch():
    s = _state()
    s.config.auto_rotation.notify_only = True
    s.tokens.mark_rate_limited("a", 300)
    s.tokens.mark_healthy("b")
    s.tokens.active = "a"
    await rotation._maybe_rotate(s)
    assert s.tokens.active == "a"
    assert s.rotation_log[-1]["action"] == "notify_only"
    assert s.rotation_log[-1]["to"] == "b"
