import asyncio
import json
import logging
import os
import time
from pathlib import Path

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("proxy")


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

def load_tokens() -> dict[str, str]:
    path = Path(__file__).parent / "tokens.yaml"
    if not path.exists():
        raise RuntimeError("tokens.yaml not found — create it with at least one named token")
    with open(path) as f:
        data = yaml.safe_load(f)
    tokens = {t["name"]: t["token"] for t in data.get("tokens", [])}
    if not tokens:
        raise RuntimeError("tokens.yaml must contain at least one token")
    return tokens


TOKENS: dict[str, str] = load_tokens()
_active: str = next(iter(TOKENS))


def active_token() -> str:
    return TOKENS[_active]


# ---------------------------------------------------------------------------
# Proxy app
# ---------------------------------------------------------------------------

API_KEYS = set(os.environ["API_KEYS"].split(","))
UPSTREAM = "https://api.anthropic.com"
client = httpx.AsyncClient(base_url=UPSTREAM, timeout=httpx.Timeout(600.0))

app = FastAPI()


def _key_label(key: str) -> str:
    return key[:8] + "..."


def _extract_model(body: bytes) -> str:
    try:
        return json.loads(body).get("model", "-")
    except Exception:
        return "-"


def build_upstream_headers(request: Request) -> dict[str, str]:
    excluded = {"host", "x-api-key", "content-length", "transfer-encoding"}
    headers = {k: v for k, v in request.headers.items() if k.lower() not in excluded}
    headers["authorization"] = f"Bearer {active_token()}"
    beta = headers.get("anthropic-beta", "")
    oauth_beta = "oauth-2025-04-20"
    if oauth_beta not in beta:
        headers["anthropic-beta"] = f"{beta},{oauth_beta}".strip(",")
    return headers


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(request: Request, path: str):
    key = request.headers.get("x-api-key")
    if not key or key not in API_KEYS:
        log.warning("REJECTED %s %s /v1/%s (invalid key)", request.client.host, request.method, path)
        return JSONResponse(status_code=401, content={"error": "Invalid API key"})

    headers = build_upstream_headers(request)
    body = await request.body()
    model = _extract_model(body) if request.method == "POST" else "-"

    log.info(
        ">>> %s %s /v1/%s  key=%s  model=%s  token=%s",
        request.client.host, request.method, path, _key_label(key), model, _active,
    )
    t0 = time.monotonic()

    req = client.build_request(
        method=request.method,
        url=f"/v1/{path}",
        headers=headers,
        content=body,
        params=request.query_params,
    )
    upstream_resp = await client.send(req, stream=True)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    content_type = upstream_resp.headers.get("content-type", "")
    is_stream = "text/event-stream" in content_type

    log.info(
        "<<< %s /v1/%s  status=%d  stream=%s  %dms",
        _key_label(key), path, upstream_resp.status_code, is_stream, elapsed_ms,
    )

    resp_headers = {
        k: v for k, v in upstream_resp.headers.items()
        if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")
    }

    async def stream_body():
        try:
            async for chunk in upstream_resp.aiter_bytes():
                yield chunk
        finally:
            await upstream_resp.aclose()

    if is_stream:
        return StreamingResponse(
            stream_body(),
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type="text/event-stream",
        )

    body = await upstream_resp.aread()
    await upstream_resp.aclose()
    return StreamingResponse(
        iter([body]),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=content_type or "application/json",
    )


# ---------------------------------------------------------------------------
# Admin app — token selector UI (served on Tailscale port)
# ---------------------------------------------------------------------------

admin_app = FastAPI()

_ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Proxy — Token Selector</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: system-ui, -apple-system, sans-serif;
    background: #0d0d0f;
    color: #d4d4d8;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .card {
    background: #18181b;
    border: 1px solid #27272a;
    border-radius: 14px;
    padding: 36px;
    width: 100%;
    max-width: 460px;
    box-shadow: 0 4px 32px rgba(0,0,0,.4);
  }
  .logo { font-size: 1.25rem; font-weight: 700; color: #fff; margin-bottom: 4px; }
  .sub { font-size: 0.82rem; color: #71717a; margin-bottom: 28px; }
  .token-list { display: flex; flex-direction: column; gap: 10px; }
  .token-btn {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 14px 18px;
    border-radius: 10px;
    border: 1.5px solid #27272a;
    background: #09090b;
    cursor: pointer;
    transition: border-color .15s, background .15s;
    text-align: left;
    width: 100%;
    color: #a1a1aa;
    font-size: 0.9rem;
  }
  .token-btn:hover { border-color: #3f3f46; background: #111113; }
  .token-btn.active { border-color: #7c3aed; background: #1c1430; color: #e4e4e7; }
  .indicator {
    width: 10px; height: 10px;
    border-radius: 50%;
    background: #3f3f46;
    flex-shrink: 0;
    transition: background .15s;
  }
  .token-btn.active .indicator { background: #7c3aed; }
  .name { font-weight: 600; flex: 1; }
  .badge {
    font-size: 0.72rem;
    padding: 2px 9px;
    border-radius: 99px;
    background: #7c3aed22;
    color: #a78bfa;
    font-weight: 500;
  }
  .feedback {
    margin-top: 18px;
    padding: 10px 14px;
    border-radius: 8px;
    font-size: 0.82rem;
    display: none;
  }
  .feedback.ok { background: #052e16; color: #4ade80; border: 1px solid #14532d; display: block; }
  .feedback.err { background: #450a0a; color: #f87171; border: 1px solid #7f1d1d; display: block; }
</style>
</head>
<body>
<div class="card">
  <div class="logo">Claude Proxy</div>
  <div class="sub">Select the OAuth token used for upstream requests. Changes take effect immediately.</div>
  <div class="token-list" id="list"></div>
  <div class="feedback" id="fb"></div>
</div>
<script>
let state = { tokens: [], active: "" };

async function init() {
  const r = await fetch("/state");
  state = await r.json();
  render();
}

function render() {
  document.getElementById("list").innerHTML = state.tokens.map(n => `
    <button class="token-btn ${n === state.active ? "active" : ""}" onclick="pick(${JSON.stringify(n)})">
      <span class="indicator"></span>
      <span class="name">${n}</span>
      ${n === state.active ? '<span class="badge">active</span>' : ""}
    </button>
  `).join("");
}

async function pick(name) {
  const fb = document.getElementById("fb");
  try {
    const r = await fetch("/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (!r.ok) throw new Error(await r.text());
    state.active = name;
    render();
    fb.textContent = `Switched to "${name}"`;
    fb.className = "feedback ok";
  } catch (e) {
    fb.textContent = `Error: ${e.message}`;
    fb.className = "feedback err";
  }
  setTimeout(() => { fb.className = "feedback"; }, 3000);
}

init();
</script>
</body>
</html>"""


@admin_app.get("/", response_class=HTMLResponse)
async def admin_index():
    return _ADMIN_HTML


@admin_app.get("/state")
async def admin_state():
    return JSONResponse({"tokens": list(TOKENS.keys()), "active": _active})


@admin_app.post("/select")
async def admin_select(request: Request):
    global _active
    body = await request.json()
    name = body.get("name", "")
    if name not in TOKENS:
        return JSONResponse(status_code=400, content={"error": f"Unknown token: {name!r}"})
    _active = name
    log.info("Token switched to: %s", name)
    return JSONResponse({"active": _active})


# ---------------------------------------------------------------------------
# Entry point — run proxy + admin servers concurrently
# ---------------------------------------------------------------------------

async def _main():
    admin_port = int(os.environ.get("ADMIN_PORT", "8090"))
    proxy_cfg = uvicorn.Config(app, host="0.0.0.0", port=8080, log_config=None)
    admin_cfg = uvicorn.Config(admin_app, host="0.0.0.0", port=admin_port, log_config=None)
    proxy_srv = uvicorn.Server(proxy_cfg)
    admin_srv = uvicorn.Server(admin_cfg)
    # Prevent the second server from overwriting the first's signal handlers
    admin_srv.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    await asyncio.gather(proxy_srv.serve(), admin_srv.serve())


if __name__ == "__main__":
    asyncio.run(_main())
