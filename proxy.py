import json
import logging
import os
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

CLAUDE_OAUTH_TOKEN = os.environ["CLAUDE_OAUTH_TOKEN"]
API_KEYS = set(os.environ["API_KEYS"].split(","))
UPSTREAM = "https://api.anthropic.com"

log = logging.getLogger("proxy")

app = FastAPI()
client = httpx.AsyncClient(base_url=UPSTREAM, timeout=httpx.Timeout(600.0))


def validate_api_key(request: Request) -> str | None:
    key = request.headers.get("x-api-key")
    if key and key in API_KEYS:
        return key
    return None


def build_upstream_headers(request: Request) -> dict[str, str]:
    excluded = {"host", "x-api-key", "content-length", "transfer-encoding"}
    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in excluded
    }
    headers["authorization"] = f"Bearer {CLAUDE_OAUTH_TOKEN}"
    # Required for OAuth token authentication
    beta = headers.get("anthropic-beta", "")
    oauth_beta = "oauth-2025-04-20"
    if oauth_beta not in beta:
        headers["anthropic-beta"] = f"{beta},{oauth_beta}".strip(",")
    return headers


def _key_label(key: str) -> str:
    """Return a short label for the API key (first 8 chars)."""
    return key[:8] + "..."


def _extract_model(body: bytes) -> str:
    try:
        return json.loads(body).get("model", "-")
    except Exception:
        return "-"


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(request: Request, path: str):
    key = validate_api_key(request)
    if not key:
        log.warning("REJECTED %s %s /v1/%s (invalid key)", request.client.host, request.method, path)
        return JSONResponse(status_code=401, content={"error": "Invalid API key"})

    headers = build_upstream_headers(request)
    body = await request.body()
    url = f"/v1/{path}"
    model = _extract_model(body) if request.method == "POST" else "-"

    log.info(">>> %s %s /v1/%s  key=%s  model=%s", request.client.host, request.method, path, _key_label(key), model)
    t0 = time.monotonic()

    req = client.build_request(
        method=request.method,
        url=url,
        headers=headers,
        content=body,
        params=request.query_params,
    )
    upstream_resp = await client.send(req, stream=True)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    content_type = upstream_resp.headers.get("content-type", "")
    is_stream = "text/event-stream" in content_type

    log.info("<<< %s /v1/%s  status=%d  stream=%s  %dms", _key_label(key), path, upstream_resp.status_code, is_stream, elapsed_ms)

    async def stream_response():
        try:
            async for chunk in upstream_resp.aiter_bytes():
                yield chunk
        finally:
            await upstream_resp.aclose()

    resp_headers = {
        k: v
        for k, v in upstream_resp.headers.items()
        if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")
    }

    if is_stream:
        return StreamingResponse(
            stream_response(),
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type="text/event-stream",
        )

    # For non-streaming, read full body and return
    body = await upstream_resp.aread()
    await upstream_resp.aclose()
    return StreamingResponse(
        iter([body]),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=content_type or "application/json",
    )
