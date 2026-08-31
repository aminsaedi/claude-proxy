"""The client-facing proxy app: authenticates virtual keys, forwards to
api.anthropic.com with an OAuth token, and transparently fails over to another
token on a retryable upstream error (429/5xx).

Everything here runs on the request hot path, so the rule throughout is that
observability work is either free or deferred: usage parsing piggybacks on
bytes we are already relaying, audit records are assembled from references to
buffers we already hold and handed to a background thread, and nothing on this
path opens a file or takes a lock it could wait on.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import secrets
import time
import zlib

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import __version__, metrics
from .audit import Record

log = logging.getLogger("claude_proxy.proxy")

RETRYABLE = {429, 500, 502, 503, 529}
MAX_ATTEMPTS = 3
_EXCLUDED_REQ_HEADERS = {
    "host", "x-api-key", "authorization", "content-length", "transfer-encoding",
    "accept-encoding",
    # Hop-by-hop headers describe *this* connection, not the forwarded request;
    # relaying them upstream is at best meaningless and at worst breaks pooling.
    "connection", "keep-alive", "upgrade", "te", "trailer", "proxy-authorization",
    "proxy-connection", "expect",
}
_EXCLUDED_RESP_HEADERS = {
    "transfer-encoding", "content-encoding", "content-length",
    "connection", "keep-alive", "upgrade", "trailer",
}
OAUTH_BETA = "oauth-2025-04-20"

# A 429 from a spend limit carries Retry-After. For a monthly cap that would
# honestly be weeks, which clients treat as "this endpoint is gone" rather than
# backing off. Cap it so a caller keeps checking back at a sane cadence.
MAX_RETRY_AFTER = 3600

# Metric label guard. `model` comes from a client-supplied request body, and
# Prometheus labels are unbounded cardinality if you let them be — one client
# looping over random model names would grow the registry without limit.
MAX_MODEL_LABELS = 200
_seen_models: set[str] = set()

# SSE event names worth the cost of a json.loads. Everything else — the ping
# events, block starts and stops — is skipped without parsing, which is most of
# the events in a long completion. `error` is in here despite carrying no usage
# because upstream reports a mid-stream failure *inside* a 200 response: the
# status line already said OK, so this frame is the only evidence the caller
# did not get what they asked for.
_PARSED_EVENTS = (b"message_start", b"message_delta", b"error")
_TEXT_EVENT = b"content_block_delta"
_STOP_EVENT = b"message_stop"


def _decode_body(body: bytes, encoding: str) -> bytes:
    """Best-effort decompression for upstreams that ignore accept-encoding.

    httpx only decodes gzip/deflate (and brotli when the optional module is
    installed); we relay bodies as-is while stripping the content-encoding
    header, so anything still compressed would corrupt the client's response.
    """
    try:
        if encoding == "gzip":
            return gzip.decompress(body)
        if encoding == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
        if encoding == "br":
            import brotli  # noqa: PLC0415

            return brotli.decompress(body)
        if encoding == "zstd":
            import zstandard  # noqa: PLC0415

            return zstandard.ZstdDecompressor().decompress(body)
    except Exception:  # noqa: BLE001
        pass
    return body


def _auth_error(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"type": "error", "error": {"type": "authentication_error", "message": message}},
    )


def _upstream_error(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={"type": "error", "error": {"type": "api_error", "message": message}},
    )


def _budget_error(status, now: float) -> JSONResponse:  # noqa: ANN001 - budgets.LimitStatus
    """429 in Anthropic's envelope, with Retry-After set to the window rollover.

    Clients already back off on 429 + Retry-After, so an over-budget key behaves
    like a rate-limited one rather than failing in some bespoke way.
    """
    until_reset = max(1, int(status.resets_at - now))
    return JSONResponse(
        status_code=429,
        content={"type": "error", "error": {
            "type": "rate_limit_error",
            "message": f"{status.message()} Resets in {_human(until_reset)}.",
        }},
        headers={
            "retry-after": str(min(until_reset, MAX_RETRY_AFTER)),
            "x-proxy-limit-period": status.period,
            "x-proxy-limit-usd": f"{status.limit_usd:.2f}",
            "x-proxy-limit-spent-usd": f"{status.spent_usd:.2f}",
            "x-proxy-limit-resets-at": str(int(status.resets_at)),
        },
    )


def _human(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


def _mask(key: str) -> str:
    return key[:6] + "…" if len(key) > 7 else "…"


def _throttled_error(retry_after: int) -> JSONResponse:
    """Anthropic's rate-limit shape, so a client's existing backoff handles it."""
    return JSONResponse(
        status_code=429,
        headers={"retry-after": str(retry_after)},
        content={"type": "error", "error": {
            "type": "rate_limit_error",
            "message": "too many failed authentication attempts; slow down",
        }},
    )


def _client_host(request: Request) -> str:
    """The caller's address, preferring what the edge says it is.

    `CF-Connecting-IP` first, and it has to be first. Traefik does not trust
    cloudflared, so it *rewrites* X-Forwarded-For to its own immediate peer —
    which is the cloudflared pod. Reading XFF therefore reported `10.42.x.y`
    for every request that came through the tunnel, collapsing the whole
    internet onto two pod IPs and making the audit trail useless for saying who
    anyone was. Cloudflare sets CF-Connecting-IP at the edge and overwrites
    whatever the client sent, so it is both correct and unspoofable here.

    XFF stays as the fallback for a caller that reaches Traefik without going
    through Cloudflare; the peer address is the last resort for in-cluster
    callers hitting the service directly.
    """
    for header in ("cf-connecting-ip", "x-forwarded-for"):
        value = request.headers.get(header)
        if value:
            first = value.split(",")[0].strip()
            if first:
                return first[:64]
    return request.client.host if request.client else "-"


def _extract_model(body: bytes) -> str:
    try:
        return json.loads(body).get("model", "-")
    except Exception:  # noqa: BLE001
        return "-"


def _wants_stream(body: bytes) -> bool:
    """Whether the caller asked for SSE, judged from the request it sent.

    Deliberately not the response's content-type: the point of asking is to
    decide whether we can answer *before* upstream has replied at all.
    """
    try:
        return json.loads(body).get("stream") is True
    except Exception:  # noqa: BLE001
        return False


def _model_label(model: str) -> str:
    """Bound the `model` metric label to models we've actually seen before."""
    if model in _seen_models:
        return model
    if len(_seen_models) >= MAX_MODEL_LABELS:
        return "other"
    _seen_models.add(model)
    return model


def _base_upstream_headers(request: Request) -> dict[str, str]:
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _EXCLUDED_REQ_HEADERS}
    beta = headers.get("anthropic-beta", "")
    if OAUTH_BETA not in beta:
        headers["anthropic-beta"] = f"{beta},{OAUTH_BETA}".strip(",")
    # Never accept compressed bodies from upstream. api.anthropic.com (behind
    # Cloudflare) brotli-compresses non-SSE JSON responses (stream:false
    # /v1/messages, /v1/models, error envelopes) when the client advertises
    # accept-encoding; this proxy relays bodies as-is but strips
    # content-encoding, which would hand the client raw brotli bytes. Request
    # identity explicitly so neither Cloudflare nor httpx negotiates one.
    headers["accept-encoding"] = "identity"
    return headers


def _retry_after(headers: httpx.Headers) -> float | None:
    ra = headers.get("retry-after")
    try:
        return float(ra) if ra is not None else None
    except ValueError:
        return None


async def _open_upstream(state, request: Request, path: str, body: bytes,  # noqa: ANN001
                         base_headers: dict[str, str], order: list[str], model: str):  # noqa: ANN202
    """Send the request upstream, failing over across tokens.

    Returns ``(response, token_name, attempts, last_error)`` with the response
    left open and unread; ``response`` is None when every token was exhausted.
    """
    upstream = None
    used = ""
    attempts = 0
    last_error = ""
    for attempt, token_name in enumerate(order):
        attempts = attempt + 1
        headers = dict(base_headers)
        try:
            headers["authorization"] = f"Bearer {state.tokens.secret(token_name)}"
        except KeyError:  # token deleted between ordering and use
            continue
        sent = time.monotonic()
        try:
            req = state.client.build_request(
                method=request.method, url=f"/v1/{path}",
                headers=headers, content=body, params=request.query_params,
            )
            resp = await state.client.send(req, stream=True)
        except httpx.HTTPError as e:
            last_error = f"{type(e).__name__}: {e}"
            log.warning("upstream error via %s: %s", token_name, e)
            continue

        latency = time.monotonic() - sent
        state.tokens.record_headers(token_name, dict(resp.headers))
        metrics.update_util_gauges(token_name, dict(resp.headers))
        status = resp.status_code

        if status == 429:
            state.tokens.mark_rate_limited(token_name, _retry_after(resp.headers))
        elif 200 <= status < 300:
            state.tokens.mark_healthy(token_name)

        if status in RETRYABLE and attempt < len(order) - 1:
            await resp.aclose()
            metrics.FAILOVERS.inc()
            last_error = f"HTTP {status}"
            log.info("failover: %s returned %d, trying next token", token_name, status)
            continue

        upstream, used = resp, token_name
        metrics.UPSTREAM_TTFB.labels(model=_model_label(model)).observe(latency)
        break

    return upstream, used, attempts, last_error


def _abort(rec: Record, t0: float, where: str) -> None:
    """Mark a record as abandoned by the caller before it could be answered.

    499 is the ingress convention for "client closed request" — the same status
    the caller's own tooling reports — so the audit ends up describing the event
    in the words the person investigating it will be searching for.
    """
    rec.status, rec.outcome = 499, "aborted"
    rec.error = f"client disconnected {where}"
    rec.latency_ms = (time.monotonic() - t0) * 1000


async def _caller_gone(request: Request) -> bool:
    """Has the caller hung up? Never raises -- a bad answer here must not fail a
    request that is otherwise fine, so anything unexpected reads as "still there"."""
    try:
        return await request.is_disconnected()
    except Exception:  # noqa: BLE001
        return False


async def _close_quietly(resp) -> None:  # noqa: ANN001 - httpx.Response
    """Release the upstream connection without letting teardown mask a failure.

    Called from except/finally paths that are already unwinding, where a second
    exception out of ``aclose`` would replace the one worth reporting.
    """
    try:
        await resp.aclose()
    except Exception:  # noqa: BLE001
        pass
    except asyncio.CancelledError:
        pass


def _sse_error(message: str, etype: str = "api_error") -> bytes:
    """An Anthropic-shaped SSE error frame.

    Once the preamble has gone out the status line is spent, so this is the
    only channel left for reporting a failure to the caller.
    """
    payload = json.dumps({"type": "error", "error": {"type": etype, "message": message}})
    return b"event: error\ndata: " + payload.encode() + b"\n\n"


async def _keepalive_stream(state, request: Request, path: str, body: bytes,  # noqa: ANN001
                            base_headers: dict[str, str], order: list[str], model: str,
                            key_name: str, rec: Record, t0: float, keepalive: int):
    """Hold the connection open with SSE comments, then relay the real stream.

    Comment frames (``: ...``) are the SSE no-op: every compliant client
    discards them, so this is invisible to the caller apart from the response
    starting immediately. Nothing here is written to the audit record that
    ``_stream_and_track`` would not have written itself.
    """
    task = asyncio.ensure_future(
        _open_upstream(state, request, path, body, base_headers, order, model))
    try:
        while True:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=keepalive)
                break
            except TimeoutError:
                yield b": waiting for upstream\n\n"
    except (asyncio.CancelledError, GeneratorExit):
        # Caller hung up while we were still waiting; don't leak the attempt.
        # This is the keepalive path's version of the vanishing request, and it
        # is the one that matters most: if the edge ever times out *despite* the
        # comment frames, this row is the evidence.
        task.cancel()
        _abort(rec, t0, "while waiting for upstream")
        _submit(state.audit, rec)
        raise

    try:
        upstream, used, attempts, last_error = task.result()
    except Exception as e:  # noqa: BLE001
        rec.status, rec.outcome = 502, "error"
        rec.error = f"{type(e).__name__}: {e}"
        rec.latency_ms = (time.monotonic() - t0) * 1000
        _submit(state.audit, rec)
        yield _sse_error(f"upstream request failed: {e}")
        return

    rec.attempts = attempts

    if upstream is None:
        rec.status, rec.outcome = 502, "error"
        rec.error = last_error or "all upstream tokens failed"
        rec.latency_ms = (time.monotonic() - t0) * 1000
        metrics.REQUESTS.labels(key_name=key_name, model=_model_label(model),
                                status="502").inc()
        _submit(state.audit, rec)
        yield _sse_error(rec.error, "api_error")
        return

    status = upstream.status_code
    is_stream = "text/event-stream" in upstream.headers.get("content-type", "")
    rec.token_name = used
    rec.status = status
    rec.streamed = is_stream
    rec.outcome = "ok" if 200 <= status < 300 else "error"
    metrics.REQUESTS.labels(key_name=key_name, model=_model_label(model),
                            status=str(status)).inc()

    if is_stream and 200 <= status < 300:
        inner = _stream_and_track(state, upstream, key_name, model, rec, t0)
        try:
            async for chunk in inner:
                yield chunk
        finally:
            # Close explicitly rather than leaving it to the garbage collector.
            # That generator's `finally` is what writes the audit row, and when
            # the caller hangs up mid-stream the GC may not get to it for an
            # arbitrarily long time — a row that lands minutes later is a row
            # that was missing when someone went looking for it.
            await inner.aclose()
        return

    # Upstream refused, or answered with a non-SSE body despite `stream: true`.
    # The 200 is already committed, so the status has to be restated in-band.
    try:
        out = await upstream.aread()
    except (asyncio.CancelledError, GeneratorExit):
        _abort(rec, t0, "while the error body was being read")
        await _close_quietly(upstream)
        _submit(state.audit, rec)
        raise
    except Exception as e:  # noqa: BLE001
        rec.outcome = "error"
        rec.error = f"upstream read failed: {type(e).__name__}: {e}"
        rec.latency_ms = (time.monotonic() - t0) * 1000
        await _close_quietly(upstream)
        _submit(state.audit, rec)
        yield _sse_error(rec.error)
        return
    await upstream.aclose()
    encoding = upstream.headers.get("content-encoding", "")
    if encoding and encoding.lower() != "identity":
        out = _decode_body(out, encoding.lower())
    rec.ttfb_ms = rec.ttfb_ms or (time.monotonic() - t0) * 1000
    rec.latency_ms = (time.monotonic() - t0) * 1000
    rec.resp_bytes = len(out)
    if state.audit.keep_bodies:
        rec.resp_body = out
    if not rec.error:
        rec.error = _error_message(out)
    _submit(state.audit, rec)
    log.warning("upstream %d under keepalive stream via %s: %s", status, used, rec.error)
    yield _sse_error(rec.error or f"upstream returned HTTP {status}")


def build_proxy_app(state) -> FastAPI:  # noqa: ANN001
    app = FastAPI(title="claude-proxy")

    @app.get("/healthz")
    async def healthz() -> Response:
        """Readiness. 503 once shutdown begins, so traffic stops being routed here.

        This is the first half of a zero-downtime rollout: failing readiness is
        what removes the pod from the ingress's backend set while it goes on
        serving whatever is already in flight.
        """
        if state.draining:
            return JSONResponse(status_code=503, content={"status": "draining"})
        return JSONResponse({"status": "ok", "active_token": state.tokens.active,
                             "version": __version__})

    @app.get("/livez")
    async def livez() -> dict:
        """Liveness — 200 for as long as the process can serve at all.

        Deliberately separate from readiness, and the distinction is not
        academic: point a liveness probe at ``/healthz`` and the 503 that a
        draining pod returns *by design* reads as "this container is wedged",
        so the kubelet kills it — cutting off exactly the in-flight streaming
        requests the drain exists to protect.
        """
        return {"status": "alive", "draining": state.draining,
                "uptime_seconds": round(time.time() - state.started_at, 1)}

    @app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def proxy(request: Request, path: str):  # noqa: ANN202
        start = time.time()
        t0 = time.monotonic()
        request_id = secrets.token_hex(8)
        audit = state.audit

        key = request.headers.get("x-api-key")
        if not key:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                key = auth[7:]
        key_name = state.vkeys.resolve(key)
        if not key_name:
            caller = _client_host(request)
            # The guard is consulted *only* on this branch, after the key has
            # already failed. An authenticated request never reaches it, so a
            # wrong idea of the caller's address can throttle an attacker but
            # can never lock out a client holding a valid key.
            guard = state.auth_guard
            wait = guard.retry_after(caller)
            if wait is None:
                metrics.AUTH_FAILURES.inc()
                tripped = guard.record_failure(caller)
                if tripped is not None:
                    log.warning("THROTTLING %s — %d invalid keys in %ds, blocking for %ds",
                                caller, guard.max_failures, guard.window_seconds, tripped)
                log.warning("REJECTED %s %s /v1/%s key=%s", caller, request.method, path,
                            _mask(key) if key else "<none>")
                # Deliberately recorded without a body: an unauthenticated caller
                # must not be able to write arbitrary bytes into the audit store.
                _submit(audit, _record(request, request_id, start, "", path,
                                       status=401, outcome="rejected",
                                       error="invalid x-api-key", t0=t0))
                return _auth_error("invalid x-api-key")
            metrics.AUTH_THROTTLED.inc()
            _submit(audit, _record(request, request_id, start, "", path,
                                   status=429, outcome="rejected",
                                   error="too many invalid keys", t0=t0))
            return _throttled_error(wait)

        # A caller that gets it right has nothing held against it.
        state.auth_guard.forget(_client_host(request))

        now = time.time()
        breach = state.limit_breach(key_name, now)
        if breach is not None:
            metrics.LIMIT_BLOCKS.labels(key_name=key_name, period=breach.period).inc()
            log.warning("BLOCKED %s(%s) /v1/%s — $%.2f of $%.2f %s",
                        _client_host(request), key_name, path,
                        breach.spent_usd, breach.limit_usd, breach.period)
            _submit(audit, _record(request, request_id, start, key_name, path,
                                   status=429, outcome="blocked",
                                   error=breach.message(), t0=t0))
            return _budget_error(breach, now)

        body = await request.body()
        model = _extract_model(body) if request.method == "POST" else "-"
        rec = _record(request, request_id, start, key_name, path, model=model, t0=None)
        if audit.enabled:
            rec.req_body = body  # a reference, not a copy — no hot-path cost
            rec.req_bytes = len(body)
        base_headers = _base_upstream_headers(request)
        base_headers["x-proxy-request-id"] = request_id
        order = state.tokens.failover_order()[:MAX_ATTEMPTS]

        # A `stream: true` caller can be answered before upstream has said
        # anything, which is the only way past an edge read timeout that large
        # contexts can otherwise exceed while Anthropic is still thinking. The
        # whole failover loop moves inside the response body for that case.
        keepalive = state.config.sse_keepalive_seconds
        if keepalive and request.method == "POST" and _wants_stream(body):
            log.info(">>> %s(%s) /v1/%s -> streaming with %ds keepalive rid=%s",
                     _client_host(request), key_name, path, keepalive, request_id)
            return StreamingResponse(
                _keepalive_stream(state, request, path, body, base_headers, order,
                                  model, key_name, rec, t0, keepalive),
                status_code=200,
                headers={"x-proxy-request-id": request_id},
                media_type="text/event-stream",
            )

        try:
            upstream, used, attempts, last_error = await _open_upstream(
                state, request, path, body, base_headers, order, model)
        except asyncio.CancelledError:
            # Cancellation lands *here*, on the send, before any response object
            # or its `finally` exists. This is the case that used to leave no
            # trace at all — the reason "no audit row" could not be read as "the
            # request never happened". Now it can.
            _abort(rec, t0, "before upstream responded")
            log.warning("ABORTED %s(%s) /v1/%s rid=%s after %.1fs",
                        _client_host(request), key_name, path, request_id,
                        (rec.latency_ms or 0) / 1000)
            _submit(audit, rec)
            raise
        except Exception as e:  # noqa: BLE001 - anything unhandled still gets a row
            rec.status, rec.outcome = 502, "error"
            rec.error = f"{type(e).__name__}: {e}"
            rec.latency_ms = (time.monotonic() - t0) * 1000
            log.exception("upstream open failed rid=%s", request_id)
            _submit(audit, rec)
            return _upstream_error(f"upstream request failed: {e}")

        rec.attempts = attempts
        rec.ttfb_ms = (time.monotonic() - t0) * 1000

        if upstream is None:
            rec.status = 502
            rec.outcome = "error"
            rec.error = last_error or "all upstream tokens failed"
            rec.latency_ms = (time.monotonic() - t0) * 1000
            metrics.REQUESTS.labels(key_name=key_name, model=_model_label(model),
                                    status="502").inc()
            _submit(audit, rec)
            return _upstream_error("all upstream tokens failed")

        status = upstream.status_code
        content_type = upstream.headers.get("content-type", "")
        is_stream = "text/event-stream" in content_type
        log.info(">>> %s(%s) /v1/%s -> %d stream=%s token=%s rid=%s",
                 _client_host(request), key_name, path, status, is_stream, used, request_id)
        metrics.REQUESTS.labels(key_name=key_name, model=_model_label(model),
                                status=str(status)).inc()

        resp_headers = {
            k: v for k, v in upstream.headers.items()
            if k.lower() not in _EXCLUDED_RESP_HEADERS
        }
        resp_headers["x-proxy-request-id"] = request_id
        resp_headers["x-proxy-upstream"] = used

        rec.token_name = used
        rec.status = status
        rec.streamed = is_stream
        rec.outcome = "ok" if 200 <= status < 300 else "error"

        if is_stream:
            return StreamingResponse(
                _stream_and_track(state, upstream, key_name, model, rec, t0),
                status_code=status, headers=resp_headers, media_type="text/event-stream",
            )

        try:
            out = await upstream.aread()
        except asyncio.CancelledError:
            _abort(rec, t0, "while the response body was being read")
            await _close_quietly(upstream)
            _submit(audit, rec)
            raise
        except Exception as e:  # noqa: BLE001 - a body that dies mid-read is a failure
            rec.status, rec.outcome = 502, "error"
            rec.error = f"upstream read failed: {type(e).__name__}: {e}"
            rec.latency_ms = (time.monotonic() - t0) * 1000
            await _close_quietly(upstream)
            log.warning("upstream read failed rid=%s: %s", request_id, e)
            _submit(audit, rec)
            return _upstream_error(rec.error)
        await upstream.aclose()
        encoding = upstream.headers.get("content-encoding", "")
        if encoding and encoding.lower() != "identity":
            out = _decode_body(out, encoding.lower())
        rec.latency_ms = (time.monotonic() - t0) * 1000
        metrics.REQUEST_LATENCY.labels(model=_model_label(model)).observe(rec.latency_ms / 1000)
        rec.resp_bytes = len(out)
        if audit.keep_bodies:
            rec.resp_body = out
        if 200 <= status < 300:
            usage = _extract_usage(out)
            if usage:
                actual_model, *counts = usage
                rec.model = actual_model or model
                rec.input_tokens, rec.output_tokens, rec.cache_read, rec.cache_creation = counts
                rec.cost_usd = await state.usage.record(key_name, rec.model, *counts)
        elif not rec.error:
            rec.error = _error_message(out)
        # A buffered response has no disconnect listener behind it the way a
        # StreamingResponse does, so nothing has noticed if the caller left while
        # we were reading upstream. Ask before claiming we delivered anything.
        # The usage above still stands -- upstream was consumed either way.
        if await _caller_gone(request):
            rec.outcome = "aborted"
            rec.error = rec.error or "client disconnected before the response was sent"
            log.info("ABORTED %s(%s) /v1/%s buffered rid=%s",
                     _client_host(request), key_name, path, request_id)
        _submit(audit, rec)
        return Response(content=out, status_code=status,
                        headers=resp_headers, media_type=content_type or "application/json")

    return app


def _record(request: Request, request_id: str, start: float, key_name: str,
            path: str, model: str = "-", status: int = 0, outcome: str = "ok",
            error: str = "", t0: float | None = None) -> Record:
    rec = Record(
        ts=start,
        request_id=request_id,
        key_name=key_name,
        method=request.method,
        path=f"/v1/{path}",
        model=model,
        client_ip=_client_host(request),
        user_agent=request.headers.get("user-agent", "")[:200],
        status=status,
        outcome=outcome,
        error=error,
    )
    if t0 is not None:
        rec.latency_ms = (time.monotonic() - t0) * 1000
    return rec


def _submit(audit, rec: Record) -> None:  # noqa: ANN001 - AuditLog
    """Record a finished request — the single point every path converges on.

    The outcome counter is incremented here rather than at each call site so
    that "every request is accounted for" is a structural property instead of a
    convention someone has to remember: a code path that concludes without
    passing through this function does not produce an audit row either, which
    makes the omission loud rather than silent.

    It is deliberately outside the try below, and ahead of it: the counter is
    the last line of defence if the audit database itself is what has failed.
    """
    try:
        metrics.OUTCOMES.labels(outcome=rec.outcome or "unknown").inc()
    except Exception:  # noqa: BLE001 - a metric must never fail a request
        pass
    try:
        audit.submit(rec)
    except Exception as e:  # noqa: BLE001 - auditing must never fail a request
        log.warning("audit submit failed: %s", e)


def _error_message(body: bytes) -> str:
    """Pull Anthropic's error message out of a failed response, for the log."""
    try:
        data = json.loads(body)
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message", ""))[:500]
    except Exception:  # noqa: BLE001
        pass
    return body[:200].decode("utf-8", "replace")


def _extract_usage(body: bytes):
    """Return (model, input, output, cache_read, cache_creation) or None."""
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001
        return None
    usage = data.get("usage")
    if not usage:
        return None
    return (
        data.get("model", ""),
        usage.get("input_tokens", 0),
        usage.get("output_tokens", 0),
        usage.get("cache_read_input_tokens", 0),
        usage.get("cache_creation_input_tokens", 0),
    )


async def _stream_and_track(state, upstream, key_name: str, req_model: str,  # noqa: ANN001
                            rec: Record, t0: float):
    """Pass SSE bytes through while parsing usage (incl. cache tokens).

    The client's bytes are yielded *before* any inspection, so nothing here sits
    between upstream and the caller. The inspection itself is kept cheap by
    dispatching on the ``event:`` line: a long completion is overwhelmingly
    ``content_block_delta`` and ``ping`` frames, and only the few notable event
    types are worth deserializing when body capture is off.

    It also decides the request's verdict, which for a stream cannot be read off
    the status line: upstream commits to ``200`` before it has generated a
    single token, so a completion that dies half-way, or that carries an
    ``error`` frame, or that the caller walks away from, all still began life as
    a success. Those three are separated out here as ``incomplete``, ``error``
    and ``aborted`` — otherwise every one of them is recorded as a clean 200 and
    the audit log agrees the user got an answer they never received.
    """
    buf = b""
    model = req_model
    inp = out = cache_r = cache_w = 0
    event = b""
    first_byte = True
    capture = state.audit.keep_bodies
    cap = state.audit.max_body_bytes
    parts: list[str] = rec.resp_parts
    captured = 0
    saw_start = saw_stop = complete = False
    try:
        async for chunk in upstream.aiter_bytes():
            yield chunk
            if first_byte:
                first_byte = False
                rec.ttfb_ms = (time.monotonic() - t0) * 1000
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if line.startswith(b"event: "):
                    event = line[7:]
                    if event == _STOP_EVENT:
                        saw_stop = True
                    continue
                if not line.startswith(b"data: ") or line == b"data: [DONE]":
                    continue
                # `event` is empty only if the producer omitted the event line;
                # in that case fall back to parsing so we don't miss usage.
                interesting = (
                    not event
                    or event in _PARSED_EVENTS
                    or (capture and event == _TEXT_EVENT)
                )
                if not interesting:
                    continue
                try:
                    data = json.loads(line[6:])
                except Exception:  # noqa: BLE001
                    continue
                etype = data.get("type")
                if etype == "message_stop":
                    saw_stop = True
                elif etype == "error":
                    # A 200 that failed anyway. Upstream is telling the caller
                    # in-band because the status line was spent long ago.
                    err = data.get("error")
                    err = err if isinstance(err, dict) else {}
                    rec.outcome = "error"
                    rec.error = str(err.get("message") or "upstream stream error")[:500]
                    log.warning("in-stream error from upstream: %s", rec.error)
                elif etype == "message_start":
                    saw_start = True
                    msg = data.get("message", {})
                    u = msg.get("usage", {})
                    inp += u.get("input_tokens", 0)
                    cache_r += u.get("cache_read_input_tokens", 0)
                    cache_w += u.get("cache_creation_input_tokens", 0)
                    if msg.get("model"):
                        model = msg["model"]
                elif etype == "message_delta":
                    # Anthropic reports output_tokens cumulatively here, so take
                    # the high-water mark rather than summing repeated deltas.
                    reported = data.get("usage", {}).get("output_tokens", 0)
                    out = max(out, reported)
                elif capture and etype == "content_block_delta" and captured < cap:
                    text = _delta_text(data.get("delta"))
                    if text:
                        parts.append(text)
                        captured += len(text)
                        if captured >= cap:
                            rec.truncated = True
        complete = True
    except (asyncio.CancelledError, GeneratorExit):
        # The caller went away mid-completion. Everything streamed so far was
        # real and is still billed; what changes is the verdict, because a
        # truncated answer recorded as `ok` is worse than no record at all.
        rec.outcome = "aborted"
        rec.error = "client disconnected mid-stream"
        raise
    except Exception as e:  # noqa: BLE001 - a broken stream still gets accounted
        rec.outcome = "error"
        rec.error = f"{type(e).__name__}: {e}"
        raise
    finally:
        rec.model = model
        rec.input_tokens, rec.output_tokens = inp, out
        rec.cache_read, rec.cache_creation = cache_r, cache_w
        rec.latency_ms = (time.monotonic() - t0) * 1000
        rec.resp_bytes = captured
        # Upstream ended the stream on its own terms but never said it was
        # finished. Only judged when we saw a `message_start`, so endpoints with
        # a different event vocabulary are not accused of truncating.
        if complete and saw_start and not saw_stop and rec.outcome == "ok":
            rec.outcome = "incomplete"
            rec.error = rec.error or "stream ended without message_stop"
        metrics.REQUEST_LATENCY.labels(model=_model_label(model)).observe(rec.latency_ms / 1000)
        # Bookkeeping is best-effort; the audit row is not. When this generator
        # is unwinding under cancellation, an await here can be cancelled in
        # turn, and losing the cost of one request is a far smaller loss than
        # losing the only record that the request failed.
        try:
            await _close_quietly(upstream)
            rec.cost_usd = await state.usage.record(
                key_name, model, inp, out, cache_r, cache_w)
        except Exception as e:  # noqa: BLE001
            log.warning("post-stream bookkeeping failed rid=%s: %s", rec.request_id, e)
        except asyncio.CancelledError:
            log.warning("post-stream bookkeeping cancelled rid=%s", rec.request_id)
        _submit(state.audit, rec)


def _delta_text(delta) -> str:  # noqa: ANN001
    """The human-readable slice of a content_block_delta, if it has one."""
    if not isinstance(delta, dict):
        return ""
    dtype = delta.get("type")
    if dtype == "text_delta":
        return delta.get("text") or ""
    if dtype == "thinking_delta":
        return delta.get("thinking") or ""
    if dtype == "input_json_delta":
        return delta.get("partial_json") or ""
    return ""
