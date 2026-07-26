from __future__ import annotations

import httpx

from claude_proxy.app import UPSTREAM, AppState
from claude_proxy.proxy_app import build_proxy_app


def _make_state(handler) -> AppState:
    state = AppState()
    state.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=UPSTREAM)
    state.probe_client = state.client
    return state


def _asgi(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_invalid_key_returns_anthropic_error_envelope():
    def handler(request):
        return httpx.Response(200, json={})

    state = _make_state(handler)
    app = build_proxy_app(state)
    async with _asgi(app) as ac:
        r = await ac.post("/v1/messages", headers={"x-api-key": "wrong"}, json={"model": "m"})
    assert r.status_code == 401
    body = r.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "authentication_error"
    await state.client.aclose()


async def test_healthz():
    state = _make_state(lambda req: httpx.Response(200, json={}))
    app = build_proxy_app(state)
    async with _asgi(app) as ac:
        r = await ac.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    await state.client.aclose()


async def test_success_records_usage_with_cache_tokens():
    def handler(request):
        assert request.headers["authorization"].startswith("Bearer sk-")
        assert "oauth-2025-04-20" in request.headers["anthropic-beta"]
        return httpx.Response(200, json={
            "model": "claude-opus-4-8",
            "usage": {
                "input_tokens": 12, "output_tokens": 34,
                "cache_read_input_tokens": 500, "cache_creation_input_tokens": 7,
            },
        })

    state = _make_state(handler)
    app = build_proxy_app(state)
    async with _asgi(app) as ac:
        r = await ac.post("/v1/messages", headers={"x-api-key": "vk-alice"},
                          json={"model": "claude-opus-4-8"})
    assert r.status_code == 200
    snap = await state.usage.snapshot()
    m = snap["alice"]["claude-opus-4-8"]
    assert m["input_tokens"] == 12
    assert m["cache_read_input_tokens"] == 500
    assert m["cache_creation_input_tokens"] == 7
    await state.client.aclose()


async def test_failover_from_rate_limited_to_healthy():
    def handler(request):
        # token 'a' (sk-a) is rate-limited; token 'b' (sk-b) works
        if request.headers["authorization"] == "Bearer sk-a":
            return httpx.Response(429, headers={"retry-after": "30"}, json={"error": "rate"})
        return httpx.Response(200, json={"model": "m", "usage": {"input_tokens": 1, "output_tokens": 1}})

    state = _make_state(handler)
    assert state.tokens.active == "a"
    app = build_proxy_app(state)
    async with _asgi(app) as ac:
        r = await ac.post("/v1/messages", headers={"x-api-key": "vk-alice"}, json={"model": "m"})
    assert r.status_code == 200  # failed over to b, client never sees the 429
    assert state.tokens.health["a"].status == "rate_limited"
    assert state.tokens.health["b"].status == "healthy"
    await state.client.aclose()


async def test_retryable_exhausted_passes_through_last_status():
    def handler(request):
        return httpx.Response(529, json={"error": "overloaded"})

    state = _make_state(handler)
    app = build_proxy_app(state)
    async with _asgi(app) as ac:
        r = await ac.post("/v1/messages", headers={"x-api-key": "vk-alice"}, json={"model": "m"})
    # every token 529s; after retries the last response is returned as-is
    assert r.status_code == 529
    await state.client.aclose()


async def test_over_spend_limit_is_rejected_before_the_upstream_call():
    reached = []

    def handler(request):
        reached.append(request.url.path)
        return httpx.Response(200, json={"model": "m", "usage": {"input_tokens": 1}})

    state = _make_state(handler)
    # $1/day cap, and alice has already spent well past it today
    state.limits.set("alice", {"day": 1.0})
    await state.usage.record("alice", "claude-opus-5", input_tokens=10_000_000)
    app = build_proxy_app(state)
    try:
        async with _asgi(app) as ac:
            r = await ac.post("/v1/messages", headers={"x-api-key": "vk-alice"},
                              json={"model": "claude-opus-5"})
        assert r.status_code == 429
        assert r.json()["error"]["type"] == "rate_limit_error"
        assert "per day" in r.json()["error"]["message"]
        # clients back off on Retry-After; it points at the window rollover
        assert 0 < int(r.headers["retry-after"]) <= 86400
        assert r.headers["x-proxy-limit-period"] == "day"
        assert not reached, "the request must not reach the upstream at all"
        # an uncapped key is unaffected
        async with _asgi(app) as ac:
            assert (await ac.post("/v1/messages", headers={"x-api-key": "vk-bob"},
                                  json={"model": "m"})).status_code == 200
    finally:
        state.limits.set("alice", {})
        await state.client.aclose()


async def test_under_the_limit_passes_through():
    state = _make_state(lambda req: httpx.Response(
        200, json={"model": "claude-opus-5", "usage": {"input_tokens": 100, "output_tokens": 10}}))
    state.limits.set("alice", {"day": 1000.0, "hour": 500.0})
    app = build_proxy_app(state)
    try:
        async with _asgi(app) as ac:
            r = await ac.post("/v1/messages", headers={"x-api-key": "vk-alice"},
                              json={"model": "claude-opus-5"})
        assert r.status_code == 200
        assert all(not s.over for s in state.limit_status("alice"))
    finally:
        state.limits.set("alice", {})
        await state.client.aclose()


async def test_connection_errors_everywhere_return_502():
    def handler(request):
        raise httpx.ConnectError("boom")

    state = _make_state(handler)
    app = build_proxy_app(state)
    async with _asgi(app) as ac:
        r = await ac.post("/v1/messages", headers={"x-api-key": "vk-alice"}, json={"model": "m"})
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "api_error"
    await state.client.aclose()
