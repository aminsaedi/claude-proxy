from __future__ import annotations

import httpx
from starlette.requests import Request

from claude_proxy.authguard import AuthGuard
from claude_proxy.proxy_app import _client_host, build_proxy_app
from test_proxy import _asgi, _make_state


def _guard(**kw):
    return AuthGuard(max_failures=3, window_seconds=60, block_seconds=120, **kw)


def test_a_caller_is_blocked_only_after_the_threshold():
    g = _guard()
    assert g.retry_after("1.2.3.4", now=0) is None
    assert g.record_failure("1.2.3.4", now=0) is None
    assert g.record_failure("1.2.3.4", now=1) is None
    assert g.record_failure("1.2.3.4", now=2) == 120, "the third trips it"
    assert g.retry_after("1.2.3.4", now=3) == 119
    assert g.retry_after("5.6.7.8", now=3) is None, "one caller must not block another"


def test_the_block_expires_on_its_own():
    g = _guard()
    for i in range(3):
        g.record_failure("1.2.3.4", now=i)
    assert g.retry_after("1.2.3.4", now=100) is not None
    assert g.retry_after("1.2.3.4", now=200) is None, "blocks lift without intervention"


def test_failures_spread_past_the_window_never_accumulate():
    """A slow trickle is a stale client, not an attack, and must not be blocked."""
    g = _guard()
    for i in range(20):
        assert g.record_failure("1.2.3.4", now=i * 61) is None
    assert g.retry_after("1.2.3.4", now=20 * 61) is None


def test_tracking_is_bounded_so_rotating_sources_cannot_exhaust_memory():
    g = AuthGuard(max_failures=3, window_seconds=60, block_seconds=120, max_tracked=64)
    for i in range(5000):
        g.record_failure(f"10.0.{i // 256}.{i % 256}", now=i)
    assert len(g._seen) <= 64, "an unbounded dict would be a better attack than the one prevented"


def test_a_blocked_caller_is_still_being_blocked_after_eviction_pressure():
    """Eviction must prefer idle entries over one that is actively attacking."""
    g = AuthGuard(max_failures=3, window_seconds=60, block_seconds=600, max_tracked=32)
    for i in range(3):
        g.record_failure("attacker", now=1000 + i)
    assert g.retry_after("attacker", now=1003) is not None
    for i in range(200):
        g.record_failure(f"10.9.{i // 256}.{i % 256}", now=1003)
    assert g.retry_after("attacker", now=1004) is not None, "the active block was evicted"


def test_disabled_guard_never_blocks_anything():
    g = AuthGuard(max_failures=0)
    for i in range(100):
        assert g.record_failure("1.2.3.4", now=i) is None
    assert g.retry_after("1.2.3.4", now=100) is None


# =====================================================================
# Caller identity — the thing the guard is keyed on
# =====================================================================

def _host(headers, peer="10.42.5.41"):
    """Run _client_host against a scope built the way an ASGI server would."""
    return _client_host(Request({
        "type": "http",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        "client": (peer, 1234),
    }))


def test_cf_connecting_ip_wins_over_the_forwarded_chain():
    """Traefik rewrites XFF to its own peer — the cloudflared pod.

    Reading XFF reported a pod IP for every request that came through the
    tunnel, which collapsed the whole internet onto two addresses.
    """
    assert _host({"cf-connecting-ip": "203.0.113.7",
                  "x-forwarded-for": "10.42.5.41"}) == "203.0.113.7"


def test_forwarded_for_is_used_when_the_edge_header_is_absent():
    assert _host({"x-forwarded-for": "198.51.100.9, 10.0.0.1"}) == "198.51.100.9"


def test_the_peer_address_is_the_last_resort():
    assert _host({}) == "10.42.5.41"


# =====================================================================
# The property that makes this safe to deploy
# =====================================================================

async def test_a_valid_key_is_never_throttled_however_bad_the_address_is():
    """Every caller sharing one address must not be able to lock each other out.

    This is not hypothetical: before CF-Connecting-IP was preferred, every
    request through the tunnel really did arrive as the same two pod IPs. The
    guard is consulted only after a key has failed to resolve, so a valid key
    is unaffected no matter how many failures that address has racked up.
    """
    state = _make_state(lambda req: httpx.Response(
        200, json={"model": "claude-opus-5", "usage": {"input_tokens": 1}}))
    state.auth_guard.configure(max_failures=3, window_seconds=300, block_seconds=300)
    app = build_proxy_app(state)
    try:
        async with _asgi(app) as ac:
            for _ in range(10):
                r = await ac.post("/v1/messages", headers={"x-api-key": "guess"},
                                  json={"model": "claude-opus-5"})
            assert r.status_code == 429, "the guesser is throttled"
            assert r.headers["retry-after"]
            assert r.json()["error"]["type"] == "rate_limit_error"

            # Same address, correct key, no delay.
            good = await ac.post("/v1/messages", headers={"x-api-key": "vk-alice"},
                                 json={"model": "claude-opus-5"})
            assert good.status_code == 200, "a legitimate client was locked out"
    finally:
        await state.client.aclose()


async def test_authenticating_clears_the_history_for_that_address():
    state = _make_state(lambda req: httpx.Response(
        200, json={"model": "claude-opus-5", "usage": {"input_tokens": 1}}))
    state.auth_guard.configure(max_failures=3, window_seconds=300, block_seconds=300)
    app = build_proxy_app(state)
    try:
        async with _asgi(app) as ac:
            for _ in range(2):
                await ac.post("/v1/messages", headers={"x-api-key": "guess"},
                              json={"model": "claude-opus-5"})
            await ac.post("/v1/messages", headers={"x-api-key": "vk-alice"},
                          json={"model": "claude-opus-5"})
            # The two earlier failures were forgiven, so this one does not trip it.
            r = await ac.post("/v1/messages", headers={"x-api-key": "guess"},
                              json={"model": "claude-opus-5"})
            assert r.status_code == 401
    finally:
        await state.client.aclose()
