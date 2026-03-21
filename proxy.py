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
# Latest anthropic-* response headers, keyed by token name
_token_headers: dict[str, dict[str, str]] = {name: {} for name in TOKENS}


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

    # Capture anthropic-* headers from each response for the admin UI
    rl = {k: v for k, v in upstream_resp.headers.items() if k.startswith("anthropic-")}
    if rl:
        _token_headers[_active] = rl

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
# Admin app — token selector + usage dashboard (served on Tailscale port)
# ---------------------------------------------------------------------------

admin_app = FastAPI()

_ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Proxy — Token Selector</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%237c3aed'/%3E%3Ctext x='16' y='22' font-family='system-ui,sans-serif' font-size='18' font-weight='700' fill='white' text-anchor='middle'%3EC%3C/text%3E%3C/svg%3E">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: system-ui, -apple-system, sans-serif;
  background: #0d0d0f;
  color: #d4d4d8;
  min-height: 100vh;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding: 40px 16px;
}
.card {
  background: #18181b;
  border: 1px solid #27272a;
  border-radius: 14px;
  padding: 32px;
  width: 100%;
  max-width: 580px;
  box-shadow: 0 4px 32px rgba(0,0,0,.4);
}
.logo { font-size: 1.2rem; font-weight: 700; color: #fff; margin-bottom: 4px; }
.sub { font-size: 0.82rem; color: #71717a; margin-bottom: 24px; }

/* token card */
.token-card {
  border: 1.5px solid #27272a;
  border-radius: 10px;
  overflow: hidden;
  margin-bottom: 10px;
  transition: border-color .15s;
}
.token-card.active { border-color: #7c3aed; }

/* header row (clickable) */
.token-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 13px 16px;
  background: #09090b;
  cursor: pointer;
  width: 100%;
  text-align: left;
  border: none;
  color: #a1a1aa;
  font-size: 0.9rem;
  transition: background .15s;
}
.token-header:hover { background: #111113; }
.token-card.active .token-header { background: #130f1e; color: #e4e4e7; }
.indicator {
  width: 9px; height: 9px;
  border-radius: 50%;
  background: #3f3f46;
  flex-shrink: 0;
}
.token-card.active .indicator { background: #7c3aed; }
.token-name { font-weight: 600; flex: 1; }
.badge-active {
  font-size: 0.7rem; padding: 2px 8px; border-radius: 99px;
  background: #7c3aed22; color: #a78bfa; font-weight: 600;
}
.badge-status {
  font-size: 0.7rem; padding: 2px 8px; border-radius: 99px; font-weight: 600;
}
.badge-allowed { background: #14532d33; color: #4ade80; }
.badge-warning  { background: #78350f33; color: #fbbf24; }
.badge-rejected { background: #7f1d1d33; color: #f87171; }
.badge-nodata   { background: #27272a;   color: #52525b; }

/* usage body */
.token-body {
  padding: 16px 18px 14px;
  background: #0d0d10;
  border-top: 1px solid #27272a;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* usage rows */
.usage-row { display: flex; flex-direction: column; gap: 5px; }
.usage-label-row {
  display: flex; align-items: center; gap: 8px;
  font-size: 0.78rem;
}
.period-label {
  font-weight: 700; color: #71717a; width: 22px; flex-shrink: 0;
}
.pct-label { font-weight: 700; font-size: 0.82rem; min-width: 36px; }
.reset-label { color: #52525b; font-size: 0.76rem; margin-left: auto; }
.bar-track {
  height: 6px; background: #27272a; border-radius: 99px; overflow: hidden;
}
.bar-fill {
  height: 100%; border-radius: 99px;
  transition: width .4s ease;
}

/* meta row */
.meta-row {
  display: flex; flex-wrap: wrap; gap: 6px 16px;
  font-size: 0.76rem; color: #71717a;
  padding-top: 4px;
  border-top: 1px solid #1f1f23;
}
.meta-item { display: flex; gap: 5px; }
.meta-key { color: #52525b; }
.meta-val { }
.meta-val.green { color: #4ade80; }
.meta-val.amber { color: #fbbf24; }
.meta-val.red   { color: #f87171; }

/* raw headers */
details { margin-top: 4px; }
summary {
  font-size: 0.76rem; color: #52525b; cursor: pointer;
  user-select: none; padding: 2px 0;
}
summary:hover { color: #71717a; }
.raw-table {
  margin-top: 8px; width: 100%; border-collapse: collapse;
  font-size: 0.72rem; font-family: monospace;
}
.raw-table td {
  padding: 3px 6px; vertical-align: top;
  border-bottom: 1px solid #1f1f23;
}
.raw-table td:first-child { color: #71717a; white-space: nowrap; padding-right: 12px; }
.raw-table td:last-child  { color: #a1a1aa; word-break: break-all; }

/* feedback */
.feedback {
  margin-top: 14px; padding: 9px 14px; border-radius: 8px; font-size: 0.82rem; display: none;
}
.feedback.ok  { background: #052e16; color: #4ade80; border: 1px solid #14532d; display: block; }
.feedback.err { background: #450a0a; color: #f87171; border: 1px solid #7f1d1d; display: block; }
</style>
</head>
<body>
<div class="card">
  <div class="logo">Claude Proxy</div>
  <div class="sub">Select the OAuth token for upstream requests. Usage data updates after each request.</div>
  <div id="list"></div>
  <div class="feedback" id="fb"></div>
</div>
<script>
let state = { tokens: [], active: "", headers: {} };

async function init() {
  const r = await fetch("/state");
  state = await r.json();
  render();
  setInterval(async () => {
    const r2 = await fetch("/state");
    state = await r2.json();
    render();
  }, 5000);
}

function fmtReset(ts) {
  if (!ts) return "";
  const secs = Math.max(0, parseInt(ts) - Date.now() / 1000);
  if (secs < 60) return "< 1 min";
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (h >= 24) { const d = Math.floor(h / 24); return `${d}d ${h % 24}h`; }
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

function barColor(util) {
  const u = parseFloat(util) || 0;
  if (u >= 0.9) return "#ef4444";
  if (u >= 0.7) return "#f59e0b";
  return "#4ade80";
}

function statusClass(s) {
  if (!s) return "badge-nodata";
  if (s === "allowed") return "badge-allowed";
  if (s.includes("warning")) return "badge-warning";
  return "badge-rejected";
}

function metaValClass(key, val) {
  if (key.includes("fallback") && val === "available") return "green";
  if (key.includes("overage-status") && val === "rejected") return "red";
  if (key.includes("overage-status") && val === "allowed") return "green";
  if (val === "allowed") return "green";
  if (val && val.includes("warning")) return "amber";
  if (val === "rejected" || val === "blocked") return "red";
  return "";
}

function usageBar(h, period) {
  const util  = h[`anthropic-ratelimit-unified-${period}-utilization`];
  const reset = h[`anthropic-ratelimit-unified-${period}-reset`];
  const status = h[`anthropic-ratelimit-unified-${period}-status`];
  if (util === undefined) return "";
  const pct = Math.round(parseFloat(util) * 100);
  const color = barColor(util);
  const label = period === "5h" ? "5h" : "7d";
  return `
    <div class="usage-row">
      <div class="usage-label-row">
        <span class="period-label">${label}</span>
        <span class="pct-label" style="color:${color}">${pct}%</span>
        <span class="badge-status ${statusClass(status)}">${status || ""}</span>
        <span class="reset-label">resets in ${fmtReset(reset)}</span>
      </div>
      <div class="bar-track">
        <div class="bar-fill" style="width:${pct}%;background:${color}"></div>
      </div>
    </div>`;
}

function renderHeaders(h) {
  if (!h || Object.keys(h).length === 0) return "";
  const skip = new Set([
    "anthropic-ratelimit-unified-5h-utilization",
    "anthropic-ratelimit-unified-7d-utilization",
    "anthropic-ratelimit-unified-5h-reset",
    "anthropic-ratelimit-unified-7d-reset",
    "anthropic-ratelimit-unified-5h-status",
    "anthropic-ratelimit-unified-7d-status",
  ]);
  const metaKeys = [
    "anthropic-ratelimit-unified-status",
    "anthropic-ratelimit-unified-representative-claim",
    "anthropic-ratelimit-unified-fallback",
    "anthropic-ratelimit-unified-fallback-percentage",
    "anthropic-ratelimit-unified-overage-status",
    "anthropic-ratelimit-unified-overage-disabled-reason",
  ];
  const metaItems = metaKeys
    .filter(k => h[k] !== undefined)
    .map(k => {
      const shortKey = k.replace("anthropic-ratelimit-unified-", "");
      const cls = metaValClass(k, h[k]);
      return `<span class="meta-item"><span class="meta-key">${shortKey}:</span> <span class="meta-val ${cls}">${h[k]}</span></span>`;
    }).join("");

  const rows = Object.entries(h)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`)
    .join("");

  return `
    ${metaItems ? `<div class="meta-row">${metaItems}</div>` : ""}
    <details>
      <summary>Raw headers (${Object.keys(h).length})</summary>
      <table class="raw-table">${rows}</table>
    </details>`;
}

function render() {
  document.getElementById("list").innerHTML = state.tokens.map(n => {
    const isActive = n === state.active;
    const h = state.headers[n] || {};
    const overallStatus = h["anthropic-ratelimit-unified-status"];
    const hasData = Object.keys(h).length > 0;
    const statusBadge = hasData
      ? `<span class="badge-status ${statusClass(overallStatus)}">${overallStatus || "?"}</span>`
      : `<span class="badge-status badge-nodata">no data</span>`;

    const body = hasData ? `
      <div class="token-body">
        ${usageBar(h, "5h")}
        ${usageBar(h, "7d")}
        ${renderHeaders(h)}
      </div>` : "";

    return `
      <div class="token-card ${isActive ? "active" : ""}">
        <button class="token-header" onclick="pick('${n}')">
          <span class="indicator"></span>
          <span class="token-name">${n}</span>
          ${isActive ? '<span class="badge-active">active</span>' : ""}
          ${statusBadge}
        </button>
        ${body}
      </div>`;
  }).join("");
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
    return JSONResponse({
        "tokens": list(TOKENS.keys()),
        "active": _active,
        "headers": _token_headers,
    })


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
