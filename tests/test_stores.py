from __future__ import annotations

import time

from claude_proxy import db
from claude_proxy.stores import TokenHealth, TokenStore, VirtualKeyStore


def test_virtual_key_resolve():
    vk = VirtualKeyStore()
    assert vk.resolve("vk-alice") == "alice"
    assert vk.resolve("vk-bob") == "bob"
    assert vk.resolve("nope") is None
    assert vk.resolve(None) is None


def test_virtual_key_hot_reload():
    vk = VirtualKeyStore()
    assert vk.resolve("vk-carol") is None
    db.add_virtual_key("carol", "vk-carol")
    try:
        assert vk.reload_if_changed() is True
        assert vk.resolve("vk-carol") == "carol"
        assert vk.resolve("vk-alice") == "alice"
        assert vk.reload_if_changed() is False  # no further change
    finally:
        db.delete_virtual_key("carol")


def test_token_health_usable():
    h = TokenHealth()
    assert h.usable()  # unchecked
    h.status = "healthy"
    assert h.usable()
    h.status = "unhealthy"
    assert not h.usable()
    h.status = "rate_limited"
    h.rate_limited_until = time.time() + 100
    assert not h.usable()
    h.rate_limited_until = time.time() - 1
    assert h.usable()  # cooldown elapsed


def test_failover_order_prefers_low_util_active_first():
    store = TokenStore()
    now = time.time()
    # both healthy; a has high util, b low util
    store.mark_healthy("a")
    store.mark_healthy("b")
    store.health["a"].headers = {"anthropic-ratelimit-unified-5h-utilization": "0.9"}
    store.health["b"].headers = {"anthropic-ratelimit-unified-5h-utilization": "0.1"}
    store.active = "a"
    order = store.failover_order(now)
    assert order[0] == "a"  # active first when usable
    assert order[1] == "b"


def test_failover_order_skips_unusable_active():
    store = TokenStore()
    now = time.time()
    store.mark_rate_limited("a", 100)
    store.mark_healthy("b")
    store.active = "a"
    order = store.failover_order(now)
    assert order[0] == "b"  # rate-limited active dropped to the back
    assert order[-1] == "a"
