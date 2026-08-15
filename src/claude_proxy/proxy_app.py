"""The client-facing proxy app: authenticates virtual keys, forwards to
api.anthropic.com with an OAuth token, and transparently fails over to another
token on a retryable upstream error (429/5xx)."""

from __future__ import annotations

import gzip
import json
import logging
import time
import zlib

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import metrics

log = logging.getLogger("claude_proxy.proxy")

RETRYABLE = {429, 500, 502, 503, 529}
MAX_ATTEMPTS = 3
_EXCLUDED_REQ_HEADERS = {
    "host", "x-api-key", "authorization", "content-length", "transfer-encoding",
    "accept-encoding",
}
_EXCLUDED_RESP_HEADERS = {"transfer-encoding", "content-encoding", "content-length"}
OAUTH_BETA = "oauth-2025-04-20"


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
    retry_after = max(1, int(status.resets_at - now))
    return JSONResponse(
        status_code=429,
        content={"type": "error", "error": {
            "type": "rate_limit_error",
            "message": f"{status.message()} Resets in {_human(retry_after)}.",
        }},
        headers={
            "retry-after": str(retry_after),
            "x-proxy-limit-period": status.period,
            "x-proxy-limit-usd": f"{status.limit_usd:.2f}",
            "x-proxy-limit-spent-usd": f"{status.spent_usd:.2f}",
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


def _client_host(request: Request) -> str:
    return request.client.host if request.client else "-"


def _extract_model(body: bytes) -> str:
    try:
        return json.loads(body).get("model", "-")
    except Exception:  # noqa: BLE001
        return "-"


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


def build_proxy_app(state) -> FastAPI:  # noqa: ANN001
    app = FastAPI(title="claude-proxy")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "active_token": state.tokens.active}

    @app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def proxy(request: Request, path: str):  # noqa: ANN202
        key = request.headers.get("x-api-key")
        if not key:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                key = auth[7:]
        key_name = state.vkeys.resolve(key)
        if not key_name:
            log.warning("REJECTED %s %s /v1/%s key=%s",
                        _client_host(request), request.method, path,
                        _mask(key) if key else "<none>")
            return _auth_error("invalid x-api-key")

        now = time.time()
        breach = state.limit_breach(key_name, now)
        if breach is not None:
            metrics.LIMIT_BLOCKS.labels(key_name=key_name, period=breach.period).inc()
            log.warning("BLOCKED %s(%s) /v1/%s — $%.2f of $%.2f %s",
                        _client_host(request), key_name, path,
                        breach.spent_usd, breach.limit_usd, breach.period)
            return _budget_error(breach, now)

        body = await request.body()
        model = _extract_model(body) if request.method == "POST" else "-"
        base_headers = _base_upstream_headers(request)
        order = state.tokens.failover_order()[:MAX_ATTEMPTS]

        upstream = None
        used = None
        for attempt, token_name in enumerate(order):
            headers = dict(base_headers)
            headers["authorization"] = f"Bearer {state.tokens.secret(token_name)}"
            t0 = time.monotonic()
            try:
                req = state.client.build_request(
                    method=request.method, url=f"/v1/{path}",
                    headers=headers, content=body, params=request.query_params,
                )
                resp = await state.client.send(req, stream=True)
            except httpx.HTTPError as e:
                log.warning("upstream error via %s: %s", token_name, e)
                continue

            latency = time.monotonic() - t0
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
                log.info("failover: %s returned %d, trying next token", token_name, status)
                continue

            upstream, used = resp, token_name
            metrics.REQUEST_LATENCY.labels(model=model).observe(latency)
            break

        if upstream is None:
            return _upstream_error("all upstream tokens failed")

        status = upstream.status_code
        content_type = upstream.headers.get("content-type", "")
        is_stream = "text/event-stream" in content_type
        log.info(">>> %s(%s) /v1/%s -> %d stream=%s token=%s",
                 _client_host(request), key_name, path, status, is_stream, used)
        metrics.REQUESTS.labels(key_name=key_name, model=model, status=str(status)).inc()

        resp_headers = {
            k: v for k, v in upstream.headers.items()
            if k.lower() not in _EXCLUDED_RESP_HEADERS
        }

        if is_stream:
            return StreamingResponse(
                _stream_and_track(state, upstream, key_name, model),
                status_code=status, headers=resp_headers, media_type="text/event-stream",
            )

        out = await upstream.aread()
        await upstream.aclose()
        encoding = upstream.headers.get("content-encoding", "")
        if encoding and encoding.lower() != "identity":
            out = _decode_body(out, encoding.lower())
        if 200 <= status < 300:
            _usage = _extract_usage(out)
            if _usage:
                actual_model, *counts = _usage
                await state.usage.record(key_name, actual_model or model, *counts)
        return Response(content=out, status_code=status,
                        headers=resp_headers, media_type=content_type or "application/json")

    return app


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


async def _stream_and_track(state, upstream, key_name: str, req_model: str):  # noqa: ANN001
    """Pass SSE bytes through while parsing usage (incl. cache tokens)."""
    buf = b""
    model = req_model
    inp = out = cache_r = cache_w = 0
    try:
        async for chunk in upstream.aiter_bytes():
            yield chunk
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data: ") or line == b"data: [DONE]":
                    continue
                try:
                    event = json.loads(line[6:])
                except Exception:  # noqa: BLE001
                    continue
                etype = event.get("type")
                if etype == "message_start":
                    msg = event.get("message", {})
                    u = msg.get("usage", {})
                    inp += u.get("input_tokens", 0)
                    cache_r += u.get("cache_read_input_tokens", 0)
                    cache_w += u.get("cache_creation_input_tokens", 0)
                    if msg.get("model"):
                        model = msg["model"]
                elif etype == "message_delta":
                    out += event.get("usage", {}).get("output_tokens", 0)
    finally:
        await upstream.aclose()
        await state.usage.record(key_name, model, inp, out, cache_r, cache_w)
