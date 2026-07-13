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
