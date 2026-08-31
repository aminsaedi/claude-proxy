from __future__ import annotations

import asyncio
import contextlib
import json
import time

import httpx

from claude_proxy.app import UPSTREAM, AppState
from claude_proxy.audit import AuditLog
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


# =====================================================================
# Audit capture on the request path
# =====================================================================

def _audited(handler, tmp_path, **kw):
    """A state whose audit log writes to a throwaway file, already running."""
    state = _make_state(handler)
    state.audit = AuditLog(tmp_path / "audit.db", **kw)
    state.audit.start()
    return state


def _wait(state, n=1, timeout=5.0):
    deadline = time.time() + timeout
    while state.audit.written < n and time.time() < deadline:
        time.sleep(0.02)
    assert state.audit.written >= n, f"audit writer stalled at {state.audit.written}/{n}"


async def test_audit_captures_a_completed_request(tmp_path):
    # A model the pricing table knows, so the recorded cost is a real number.
    state = _audited(lambda req: httpx.Response(200, json={
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": "the answer"}],
        "usage": {"input_tokens": 12, "output_tokens": 34,
                  "cache_read_input_tokens": 5, "cache_creation_input_tokens": 1},
    }), tmp_path)
    app = build_proxy_app(state)
    try:
        async with _asgi(app) as ac:
            r = await ac.post("/v1/messages", headers={"x-api-key": "vk-alice"},
                              json={"model": "claude-opus-5",
                                    "messages": [{"role": "user", "content": "what is 2+2"}]})
        assert r.status_code == 200
        # the correlation id is echoed so a client can quote it in a bug report
        assert r.headers["x-proxy-request-id"]
        assert r.headers["x-proxy-upstream"] == "a"

        _wait(state)
        row = state.audit.query()[0]
        assert row["key_name"] == "alice"
        assert row["model"] == "claude-opus-5"
        assert row["status"] == 200 and row["outcome"] == "ok"
        assert row["input_tokens"] == 12 and row["output_tokens"] == 34
        assert row["cost_usd"] > 0            # priced from the same lookup as usage
        assert row["latency_ms"] is not None and row["ttfb_ms"] is not None
        assert row["summary"] == "what is 2+2"
        assert row["token_name"] == "a"
        assert row["request_id"] == r.headers["x-proxy-request-id"]

        full = state.audit.get(row["id"])
        assert full["request"]["messages"][0]["content"] == "what is 2+2"
        assert "the answer" in full["response"]
    finally:
        state.audit.stop()
        await state.client.aclose()


async def test_audit_records_a_budget_block_without_calling_upstream(tmp_path):
    state = _audited(lambda req: httpx.Response(200, json={}), tmp_path)
    state.limits.set("alice", {"day": 1.0})
    await state.usage.record("alice", "claude-opus-5", input_tokens=10_000_000)
    app = build_proxy_app(state)
    try:
        async with _asgi(app) as ac:
            r = await ac.post("/v1/messages", headers={"x-api-key": "vk-alice"},
                              json={"model": "claude-opus-5"})
        assert r.status_code == 429
        _wait(state)
        row = state.audit.query()[0]
        assert row["outcome"] == "blocked" and row["status"] == 429
        assert "Spend limit" in row["error"]
    finally:
        state.limits.set("alice", {})
        state.audit.stop()
        await state.client.aclose()


async def test_audit_records_a_rejection_but_never_its_body(tmp_path):
    """An unauthenticated caller must not be able to write into the audit store."""
    state = _audited(lambda req: httpx.Response(200, json={}), tmp_path)
    app = build_proxy_app(state)
    try:
        async with _asgi(app) as ac:
            r = await ac.post("/v1/messages", headers={"x-api-key": "nope"},
                              json={"messages": [{"role": "user", "content": "x" * 5000}]})
        assert r.status_code == 401
        _wait(state)
        row = state.audit.query()[0]
        assert row["outcome"] == "rejected" and row["status"] == 401
        assert not row["summary"]
        assert state.audit.get(row["id"])["request"] is None
    finally:
        state.audit.stop()
        await state.client.aclose()


async def test_audit_off_adds_no_rows(tmp_path):
    state = _audited(lambda req: httpx.Response(
        200, json={"model": "m", "usage": {"input_tokens": 1, "output_tokens": 1}}),
        tmp_path, mode="off")
    app = build_proxy_app(state)
    try:
        async with _asgi(app) as ac:
            assert (await ac.post("/v1/messages", headers={"x-api-key": "vk-alice"},
                                  json={"model": "m"})).status_code == 200
        await asyncio.sleep(0.2)   # give a writer that shouldn't exist a chance to run
        assert state.audit.query() == []
        # usage accounting is independent of auditing and must still be booked
        assert (await state.usage.snapshot())["alice"]["m"]["requests"] >= 1
    finally:
        state.audit.stop()
        await state.client.aclose()


# =====================================================================
# Streaming
# =====================================================================

_SSE = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":{"model":"claude-opus-5","usage":'
    b'{"input_tokens":7,"cache_read_input_tokens":11,"cache_creation_input_tokens":2}}}\n\n'
    b'event: ping\ndata: {"type":"ping"}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hel"}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"lo"}}\n\n'
    b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":3}}\n\n'
    b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":9}}\n\n'
    # Real Anthropic streams always close with message_stop, and the proxy
    # treats its absence as a truncated answer — so a fixture without one is
    # not a simplification, it is a different scenario.
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    b'data: [DONE]\n\n'
)


def _sse_handler(request):
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_SSE)


async def test_streaming_passes_through_and_captures_text(tmp_path):
    state = _audited(_sse_handler, tmp_path)
    app = build_proxy_app(state)
    try:
        async with _asgi(app) as ac:
            r = await ac.post("/v1/messages", headers={"x-api-key": "vk-alice"},
                              json={"model": "claude-opus-5", "stream": True,
                                    "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
        assert r.content == _SSE, "the client's bytes must be relayed untouched"

        _wait(state)
        row = state.audit.query()[0]
        assert row["streamed"] == 1
        assert row["input_tokens"] == 7
        assert row["cache_read_input_tokens"] == 11
        # message_delta reports output_tokens cumulatively, so the high-water
        # mark is the true count — summing them would give 12, not 9.
        assert row["output_tokens"] == 9
        assert state.audit.get(row["id"])["response"] == "Hello"
    finally:
        state.audit.stop()
        await state.client.aclose()


async def test_streaming_books_usage_once(tmp_path):
    state = _audited(_sse_handler, tmp_path)
    app = build_proxy_app(state)
    try:
        async with _asgi(app) as ac:
            await ac.post("/v1/messages", headers={"x-api-key": "vk-alice"},
                          json={"model": "claude-opus-5", "stream": True})
        snap = await state.usage.snapshot()
        m = snap["alice"]["claude-opus-5"]
        assert m["requests"] == 1 and m["output_tokens"] == 9
    finally:
        state.audit.stop()
        await state.client.aclose()


# =====================================================================
# Draining (zero-downtime rollout)
# =====================================================================

async def test_healthz_reports_draining_so_the_ingress_removes_the_pod():
    state = _make_state(lambda req: httpx.Response(200, json={}))
    app = build_proxy_app(state)
    try:
        async with _asgi(app) as ac:
            assert (await ac.get("/healthz")).status_code == 200
            state.draining = True
            r = await ac.get("/healthz")
            assert r.status_code == 503
            assert r.json()["status"] == "draining"
            # Traffic already in flight is still served — only readiness changes.
            assert (await ac.post("/v1/messages", headers={"x-api-key": "vk-alice"},
                                  json={"model": "m"})).status_code == 200
    finally:
        await state.client.aclose()


# =====================================================================
# SSE keepalive: answering before upstream has spoken
# =====================================================================
#
# Cloudflare's proxy read timeout is a deadline on time-to-first-byte, not on
# total duration, and a large-context request can leave Anthropic silent for
# long enough to trip it. These cover the preamble that buys past it.

def _slow_sse_handler(delay: float):
    async def handler(request):
        await asyncio.sleep(delay)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_SSE)
    return handler


async def test_keepalive_off_by_default_leaves_streaming_untouched(tmp_path):
    state = _audited(_sse_handler, tmp_path)
    assert state.config.sse_keepalive_seconds == 0
    app = build_proxy_app(state)
    try:
        async with _asgi(app) as ac:
            r = await ac.post("/v1/messages", headers={"x-api-key": "vk-alice"},
                              json={"model": "claude-opus-5", "stream": True})
        assert r.status_code == 200
        assert r.content == _SSE, "no preamble may appear while the feature is off"
    finally:
        await state.client.aclose()


async def test_keepalive_emits_comments_while_upstream_is_silent(tmp_path):
    """The whole point: bytes reach the caller before upstream has replied."""
    # Upstream must outlast the keepalive interval, or no comment is ever due.
    state = _audited(_slow_sse_handler(1.4), tmp_path)
    state.config.sse_keepalive_seconds = 1
    app = build_proxy_app(state)
    try:
        async with _asgi(app) as ac:
            async with ac.stream("POST", "/v1/messages", headers={"x-api-key": "vk-alice"},
                                 json={"model": "claude-opus-5", "stream": True}) as r:
                assert r.status_code == 200
                assert "text/event-stream" in r.headers["content-type"]
                first = None
                chunks = []
                async for chunk in r.aiter_bytes():
                    if first is None:
                        first = chunk
                    chunks.append(chunk)
        body = b"".join(chunks)
        # A comment frame is the SSE no-op, so the real stream is still intact.
        assert body.startswith(b":"), f"expected a preamble comment, got {body[:40]!r}"
        assert _SSE in body, "the upstream stream must still arrive verbatim"

        _wait(state)
        row = state.audit.query()[0]
        assert row["status"] == 200
        assert row["streamed"] == 1
        assert row["input_tokens"] == 7, "usage accounting survives the preamble"
    finally:
        await state.client.aclose()


async def test_keepalive_reports_upstream_failure_as_an_sse_error_event(tmp_path):
    """The 200 is already spent, so a refusal has to come back in-band."""
    def handler(request):
        return httpx.Response(400, json={"error": {"message": "prompt is too long"}})

    state = _audited(handler, tmp_path)
    state.config.sse_keepalive_seconds = 1
    app = build_proxy_app(state)
    try:
        async with _asgi(app) as ac:
            r = await ac.post("/v1/messages", headers={"x-api-key": "vk-alice"},
                              json={"model": "claude-opus-5", "stream": True})
        assert r.status_code == 200, "committed before upstream's status was known"
        assert b"event: error" in r.content
        assert b"prompt is too long" in r.content

        _wait(state)
        row = state.audit.query()[0]
        assert row["status"] == 400, "the audit keeps the real upstream status"
        assert row["outcome"] == "error"
    finally:
        await state.client.aclose()


async def test_keepalive_reports_total_upstream_failure_in_band(tmp_path):
    def handler(request):
        raise httpx.ConnectError("boom")

    state = _audited(handler, tmp_path)
    state.config.sse_keepalive_seconds = 1
    app = build_proxy_app(state)
    try:
        async with _asgi(app) as ac:
            r = await ac.post("/v1/messages", headers={"x-api-key": "vk-alice"},
                              json={"model": "claude-opus-5", "stream": True})
        assert r.status_code == 200
        assert b"event: error" in r.content
        _wait(state)
        assert state.audit.query()[0]["status"] == 502
    finally:
        await state.client.aclose()


async def test_keepalive_does_not_touch_non_streaming_requests(tmp_path):
    """`stream` absent means the status line is still ours to use."""
    def handler(request):
        return httpx.Response(400, json={"error": {"message": "nope"}})

    state = _audited(handler, tmp_path)
    state.config.sse_keepalive_seconds = 1
    app = build_proxy_app(state)
    try:
        async with _asgi(app) as ac:
            r = await ac.post("/v1/messages", headers={"x-api-key": "vk-alice"},
                              json={"model": "claude-opus-5"})
        assert r.status_code == 400, "a non-stream caller still gets a real status"
    finally:
        await state.client.aclose()


# =====================================================================
# Failure visibility
#
# A streaming request commits to `200 text/event-stream` before a single token
# exists, so the status line cannot be the verdict. Everything below is a way
# for a caller to end up without the answer they asked for while the status
# says otherwise — each one used to be recorded as a clean success, or as
# nothing at all.
# =====================================================================

_SSE_TRUNCATED = _SSE.replace(b'event: message_stop\ndata: {"type":"message_stop"}\n\n', b"")

_SSE_WITH_ERROR = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":{"model":"claude-opus-5","usage":'
    b'{"input_tokens":7}}}\n\n'
    b'event: error\n'
    b'data: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}\n\n'
)


async def test_a_stream_that_stops_early_is_not_recorded_as_success(tmp_path):
    """Upstream hung up mid-answer. The 200 is already spent; the row must not agree."""
    state = _audited(lambda req: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=_SSE_TRUNCATED), tmp_path)
    app = build_proxy_app(state)
    try:
        async with _asgi(app) as ac:
            r = await ac.post("/v1/messages", headers={"x-api-key": "vk-alice"},
                              json={"model": "claude-opus-5", "stream": True})
        assert r.status_code == 200, "the bytes already went out; we cannot restate the status"
        _wait(state)
        row = state.audit.query(limit=1)[0]
        assert row["outcome"] == "incomplete", row
        assert "message_stop" in row["error"]
    finally:
        await state.client.aclose()


async def test_an_in_stream_error_event_is_not_recorded_as_success(tmp_path):
    """Anthropic reports a mid-stream failure inside a 200. So must the audit."""
    state = _audited(lambda req: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=_SSE_WITH_ERROR), tmp_path)
    app = build_proxy_app(state)
    try:
        async with _asgi(app) as ac:
            r = await ac.post("/v1/messages", headers={"x-api-key": "vk-alice"},
                              json={"model": "claude-opus-5", "stream": True})
        assert r.status_code == 200
        _wait(state)
        row = state.audit.query(limit=1)[0]
        assert row["outcome"] == "error", row
        assert row["error"] == "Overloaded"
        # Still a 200 on the wire — which is exactly why `status` alone is not
        # enough to find these, and why the outcome column has to carry it.
        assert row["status"] == 200
    finally:
        await state.client.aclose()


async def test_a_caller_that_walks_away_mid_stream_is_recorded_as_aborted(tmp_path):
    """Closing the tracker early must still write a row, and not call it ok.

    Driven against the generator directly rather than through a client, because
    what is being tested is precisely what happens when nobody is left to
    receive the response — a condition an HTTP client cannot reliably stage.
    """
    from claude_proxy.audit import Record
    from claude_proxy.proxy_app import _stream_and_track

    state = _audited(_sse_handler, tmp_path)
    try:
        req = state.client.build_request("POST", "/v1/messages")
        upstream = await state.client.send(req, stream=True)
        rec = Record(ts=time.time(), request_id="deadbeef", key_name="alice")
        gen = _stream_and_track(state, upstream, "alice", "claude-opus-5",
                                rec, time.monotonic())
        async for _chunk in gen:
            break  # the caller is gone after the first chunk
        await gen.aclose()

        _wait(state)
        row = state.audit.query(limit=1)[0]
        assert row["outcome"] == "aborted", row
        assert "disconnected" in row["error"]
    finally:
        await state.client.aclose()


async def test_a_caller_that_hangs_up_before_upstream_replies_leaves_a_row(tmp_path):
    """The case that used to vanish: cancelled on the send, before any response.

    This is the shape of a Cloudflare read timeout, and for months it produced
    no log line and no audit row — which is why an empty audit log could not be
    read as "the request never happened".
    """
    state = _audited(_slow_sse_handler(5), tmp_path)
    app = build_proxy_app(state)
    try:
        async with _asgi(app) as ac:
            task = asyncio.ensure_future(
                ac.post("/v1/messages", headers={"x-api-key": "vk-alice"},
                        json={"model": "claude-opus-5", "messages": []}))
            await asyncio.sleep(0.3)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
                await task

        _wait(state)
        row = state.audit.query(limit=1)[0]
        assert row["outcome"] == "aborted", row
        assert row["status"] == 499, "the audit should speak the same language as the caller"
        assert row["ttfb_ms"] is None, "it never got a response to time"
    finally:
        await state.client.aclose()


async def test_the_failed_filter_returns_every_kind_of_failure(tmp_path):
    """One query for "what went wrong", so watching cannot miss a category."""
    state = _audited(_sse_handler, tmp_path)
    app = build_proxy_app(state)
    try:
        async with _asgi(app) as ac:
            # One success, one rejection — different outcomes, one filter.
            await ac.post("/v1/messages", headers={"x-api-key": "vk-alice"},
                          json={"model": "claude-opus-5", "stream": True})
            await ac.post("/v1/messages", headers={"x-api-key": "nope"},
                          json={"model": "claude-opus-5"})
        _wait(state, 2)
        assert len(state.audit.query(limit=50)) == 2
        failed = state.audit.query(limit=50, failed=True)
        assert [r["outcome"] for r in failed] == ["rejected"], failed
    finally:
        await state.client.aclose()


async def test_a_buffered_answer_to_a_caller_who_left_is_not_recorded_as_ok(tmp_path):
    """A non-streaming response has no disconnect listener behind it.

    A StreamingResponse races the body against the disconnect message, so it
    finds out; a plain Response just writes into the void and used to report
    `ok` for an answer nobody received. Driven over raw ASGI because staging a
    disconnect is the whole point, and an HTTP client cannot schedule one.
    """
    state = _audited(lambda req: httpx.Response(200, json={
        "model": "claude-opus-5",
        "usage": {"input_tokens": 12, "output_tokens": 34},
    }), tmp_path)
    app = build_proxy_app(state)
    body = json.dumps({"model": "claude-opus-5", "messages": []}).encode()
    events = [
        {"type": "http.request", "body": body, "more_body": False},
        {"type": "http.disconnect"},
    ]

    async def receive():
        return events.pop(0) if events else {"type": "http.disconnect"}

    async def send(_message):
        pass

    try:
        await app({
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/v1/messages", "raw_path": b"/v1/messages", "root_path": "",
            "query_string": b"", "client": ("127.0.0.1", 5555), "server": ("t", 80),
            "headers": [(b"host", b"t"), (b"x-api-key", b"vk-alice"),
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())],
        }, receive, send)

        _wait(state)
        row = state.audit.query(limit=1)[0]
        assert row["outcome"] == "aborted", row
        assert row["status"] == 200, "the upstream status is still worth knowing"
        # Upstream was consumed whether or not anyone read the answer, so the
        # spend has to be booked against the key regardless.
        assert row["input_tokens"] == 12 and row["output_tokens"] == 34, row
    finally:
        await state.client.aclose()
