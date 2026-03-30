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
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from prometheus_client import (
    Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST, REGISTRY
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("proxy")


# ---------------------------------------------------------------------------
# Upstream OAuth token management
# ---------------------------------------------------------------------------

def load_tokens() -> tuple[dict[str, str], str]:
    """Returns (name->token dict, default_token_name)."""
    path = Path(__file__).parent / "tokens.yaml"
    if not path.exists():
        raise RuntimeError("tokens.yaml not found — create it with at least one named token")
    with open(path) as f:
        data = yaml.safe_load(f)
    entries = data.get("tokens", [])
    tokens = {t["name"]: t["token"] for t in entries}
    if not tokens:
        raise RuntimeError("tokens.yaml must contain at least one token")
    # Find the entry marked default: true; fall back to first entry
    default = next((t["name"] for t in entries if t.get("default")), next(iter(tokens)))
    if default not in tokens:
        raise RuntimeError(f"Default token {default!r} not found in tokens list")
    log.info("Loaded %d token(s), default: %s", len(tokens), default)
    return tokens, default


TOKENS: dict[str, str]
TOKENS, _active = load_tokens()
_token_headers: dict[str, dict[str, str]] = {name: {} for name in TOKENS}


def active_token() -> str:
    return TOKENS[_active]


# ---------------------------------------------------------------------------
# Virtual API key management (downstream client keys)
# ---------------------------------------------------------------------------

def load_virtual_keys() -> dict[str, str]:
    """Load named virtual keys from virtual_keys.yaml, fallback to API_KEYS env var."""
    path = Path(__file__).parent / "virtual_keys.yaml"
    if path.exists():
        with open(path) as f:
            data = yaml.safe_load(f)
        keys = {vk["name"]: vk["key"] for vk in data.get("virtual_keys", [])}
        if keys:
            return keys
    # Fallback: env var (legacy)
    raw = os.environ.get("API_KEYS", "")
    if not raw:
        raise RuntimeError("No virtual keys configured: create virtual_keys.yaml or set API_KEYS env var")
    parts = [k.strip() for k in raw.split(",") if k.strip()]
    return {(f"key{i}" if i > 0 else "default"): k for i, k in enumerate(parts)}


VIRTUAL_KEYS: dict[str, str] = load_virtual_keys()          # name -> key
_VKEY_LOOKUP: dict[str, str] = {v: k for k, v in VIRTUAL_KEYS.items()}  # key -> name


# ---------------------------------------------------------------------------
# Usage tracking (per virtual key, per model)
# ---------------------------------------------------------------------------

USAGE_FILE = Path(__file__).parent / "usage_stats.json"


def _load_usage() -> dict:
    if USAGE_FILE.exists():
        try:
            return json.loads(USAGE_FILE.read_text())
        except Exception:
            pass
    return {}


_usage_stats: dict[str, dict[str, dict[str, int]]] = _load_usage()


def _save_usage() -> None:
    try:
        USAGE_FILE.write_text(json.dumps(_usage_stats, indent=2))
    except Exception as e:
        log.warning("Failed to save usage stats: %s", e)


def record_usage(key_name: str, model: str, input_tokens: int, output_tokens: int) -> None:
    if not key_name or (input_tokens == 0 and output_tokens == 0):
        return
    model = model or "unknown"
    if key_name not in _usage_stats:
        _usage_stats[key_name] = {}
    if model not in _usage_stats[key_name]:
        _usage_stats[key_name][model] = {"input_tokens": 0, "output_tokens": 0, "requests": 0}
    _usage_stats[key_name][model]["input_tokens"] += input_tokens
    _usage_stats[key_name][model]["output_tokens"] += output_tokens
    _usage_stats[key_name][model]["requests"] += 1
    PROM_INPUT_TOKENS.labels(key_name=key_name, model=model).inc(input_tokens)
    PROM_OUTPUT_TOKENS.labels(key_name=key_name, model=model).inc(output_tokens)
    log.info("USAGE key=%s model=%s in=%d out=%d", key_name, model, input_tokens, output_tokens)
    _save_usage()


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

PROM_REQUESTS = Counter(
    "proxy_requests_total", "Total proxy requests by virtual key, model, and HTTP status",
    ["key_name", "model", "status"],
)
PROM_INPUT_TOKENS = Counter(
    "proxy_input_tokens_total", "Input tokens consumed per virtual key and model",
    ["key_name", "model"],
)
PROM_OUTPUT_TOKENS = Counter(
    "proxy_output_tokens_total", "Output tokens consumed per virtual key and model",
    ["key_name", "model"],
)
PROM_UPSTREAM_UTIL_5H = Gauge(
    "proxy_upstream_utilization_5h_ratio", "Upstream OAuth token 5-hour utilization ratio",
    ["token_name"],
)
PROM_UPSTREAM_UTIL_7D = Gauge(
    "proxy_upstream_utilization_7d_ratio", "Upstream OAuth token 7-day utilization ratio",
    ["token_name"],
)


def _update_util_gauges(token_name: str, headers: dict[str, str]) -> None:
    try:
        u5h = headers.get("anthropic-ratelimit-unified-5h-utilization")
        if u5h is not None:
            PROM_UPSTREAM_UTIL_5H.labels(token_name=token_name).set(float(u5h))
        u7d = headers.get("anthropic-ratelimit-unified-7d-utilization")
        if u7d is not None:
            PROM_UPSTREAM_UTIL_7D.labels(token_name=token_name).set(float(u7d))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Proxy app
# ---------------------------------------------------------------------------

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
    excluded = {"host", "x-api-key", "authorization", "content-length", "transfer-encoding"}
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
    if not key:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:]
    key_name = _VKEY_LOOKUP.get(key) if key else None
    if not key_name:
        log.warning("REJECTED %s %s /v1/%s key=%r auth=%r", request.client.host, request.method, path, key, request.headers.get("authorization", "")[:30])
        return JSONResponse(status_code=401, content={"error": "Invalid API key"})

    headers = build_upstream_headers(request)
    body = await request.body()
    model = _extract_model(body) if request.method == "POST" else "-"

    log.info(
        ">>> %s %s /v1/%s  key=%s(%s)  model=%s  token=%s",
        request.client.host, request.method, path, _key_label(key), key_name, model, _active,
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
    status = upstream_resp.status_code

    log.info(
        "<<< %s(%s) /v1/%s  status=%d  stream=%s  %dms",
        _key_label(key), key_name, path, status, is_stream, elapsed_ms,
    )

    # Capture anthropic-* rate-limit headers and update Prometheus gauges
    rl = {k: v for k, v in upstream_resp.headers.items() if k.startswith("anthropic-")}
    if rl:
        _token_headers[_active] = rl
        _update_util_gauges(_active, rl)

    PROM_REQUESTS.labels(key_name=key_name, model=model, status=str(status)).inc()

    resp_headers = {
        k: v for k, v in upstream_resp.headers.items()
        if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")
    }

    if is_stream:
        return StreamingResponse(
            _stream_and_track(upstream_resp, key_name, model),
            status_code=status,
            headers=resp_headers,
            media_type="text/event-stream",
        )

    body = await upstream_resp.aread()
    await upstream_resp.aclose()

    # Extract and record token usage from non-streaming response
    if status == 200:
        try:
            data = json.loads(body)
            usage = data.get("usage", {})
            if usage:
                actual_model = data.get("model", model)
                record_usage(key_name, actual_model,
                             usage.get("input_tokens", 0),
                             usage.get("output_tokens", 0))
        except Exception:
            pass

    return StreamingResponse(
        iter([body]),
        status_code=status,
        headers=resp_headers,
        media_type=content_type or "application/json",
    )


async def _stream_and_track(upstream_resp, key_name: str, req_model: str):
    """Stream response bytes while parsing SSE events to extract token usage."""
    line_buf = b""
    input_tokens = 0
    output_tokens = 0
    resp_model = req_model

    try:
        async for chunk in upstream_resp.aiter_bytes():
            yield chunk
            line_buf += chunk
            while b"\n" in line_buf:
                line, line_buf = line_buf.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data: ") or line == b"data: [DONE]":
                    continue
                try:
                    event = json.loads(line[6:])
                    etype = event.get("type")
                    if etype == "message_start":
                        msg = event.get("message", {})
                        usage = msg.get("usage", {})
                        input_tokens += usage.get("input_tokens", 0)
                        if msg.get("model"):
                            resp_model = msg["model"]
                    elif etype == "message_delta":
                        usage = event.get("usage", {})
                        output_tokens += usage.get("output_tokens", 0)
                except Exception:
                    pass
    finally:
        await upstream_resp.aclose()

    record_usage(key_name, resp_model, input_tokens, output_tokens)


# ---------------------------------------------------------------------------
# Admin app — token selector + usage dashboard (served on Tailscale port)
# ---------------------------------------------------------------------------

admin_app = FastAPI()

_ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Proxy — Admin</title>
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
.page { width: 100%; max-width: 640px; display: flex; flex-direction: column; gap: 24px; }
.card {
  background: #18181b;
  border: 1px solid #27272a;
  border-radius: 14px;
  padding: 28px 32px;
  box-shadow: 0 4px 32px rgba(0,0,0,.4);
}
.section-title { font-size: 0.72rem; font-weight: 700; color: #52525b; letter-spacing: .08em; text-transform: uppercase; margin-bottom: 14px; }
.logo { font-size: 1.2rem; font-weight: 700; color: #fff; margin-bottom: 4px; }
.sub { font-size: 0.82rem; color: #71717a; }

/* ── OAuth token cards ── */
.token-card {
  border: 1.5px solid #27272a;
  border-radius: 10px;
  overflow: hidden;
  margin-bottom: 10px;
  transition: border-color .15s;
}
.token-card.active { border-color: #7c3aed; }
.token-header {
  display: flex; align-items: center; gap: 12px;
  padding: 13px 16px;
  background: #09090b;
  cursor: pointer; width: 100%; text-align: left;
  border: none; color: #a1a1aa; font-size: 0.9rem;
  transition: background .15s;
}
.token-header:hover { background: #111113; }
.token-card.active .token-header { background: #130f1e; color: #e4e4e7; }
.indicator { width: 9px; height: 9px; border-radius: 50%; background: #3f3f46; flex-shrink: 0; }
.token-card.active .indicator { background: #7c3aed; }
.token-name { font-weight: 600; flex: 1; }
.badge-active { font-size: 0.7rem; padding: 2px 8px; border-radius: 99px; background: #7c3aed22; color: #a78bfa; font-weight: 600; }
.badge-status { font-size: 0.7rem; padding: 2px 8px; border-radius: 99px; font-weight: 600; }
.badge-allowed  { background: #14532d33; color: #4ade80; }
.badge-warning  { background: #78350f33; color: #fbbf24; }
.badge-rejected { background: #7f1d1d33; color: #f87171; }
.badge-nodata   { background: #27272a;   color: #52525b; }
.token-body { padding: 16px 18px 14px; background: #0d0d10; border-top: 1px solid #27272a; display: flex; flex-direction: column; gap: 12px; }
.usage-row { display: flex; flex-direction: column; gap: 5px; }
.usage-label-row { display: flex; align-items: center; gap: 8px; font-size: 0.78rem; }
.period-label { font-weight: 700; color: #71717a; width: 22px; flex-shrink: 0; }
.pct-label { font-weight: 700; font-size: 0.82rem; min-width: 36px; }
.reset-label { color: #52525b; font-size: 0.76rem; margin-left: auto; }
.bar-track { height: 6px; background: #27272a; border-radius: 99px; overflow: hidden; }
.bar-fill  { height: 100%; border-radius: 99px; transition: width .4s ease; }
.meta-row  { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 0.76rem; color: #71717a; padding-top: 4px; border-top: 1px solid #1f1f23; }
.meta-item { display: flex; gap: 5px; }
.meta-key  { color: #52525b; }
.meta-val.green { color: #4ade80; }
.meta-val.amber { color: #fbbf24; }
.meta-val.red   { color: #f87171; }
details { margin-top: 4px; }
summary { font-size: 0.76rem; color: #52525b; cursor: pointer; user-select: none; padding: 2px 0; }
summary:hover { color: #71717a; }
.raw-table { margin-top: 8px; width: 100%; border-collapse: collapse; font-size: 0.72rem; font-family: monospace; }
.raw-table td { padding: 3px 6px; vertical-align: top; border-bottom: 1px solid #1f1f23; }
.raw-table td:first-child { color: #71717a; white-space: nowrap; padding-right: 12px; }
.raw-table td:last-child  { color: #a1a1aa; word-break: break-all; }

/* ── Virtual key cards ── */
.vkey-card {
  border: 1px solid #27272a;
  border-radius: 10px;
  overflow: hidden;
  margin-bottom: 10px;
}
.vkey-header {
  display: flex; align-items: center; gap: 12px;
  padding: 12px 16px;
  background: #09090b;
}
.vkey-name { font-weight: 700; font-size: 0.92rem; color: #e4e4e7; flex: 1; }
.vkey-totals { display: flex; gap: 14px; font-size: 0.75rem; }
.vkey-stat { display: flex; flex-direction: column; align-items: flex-end; }
.vkey-stat-val { font-weight: 700; color: #e4e4e7; }
.vkey-stat-lbl { font-size: 0.68rem; color: #52525b; }
.vkey-body { padding: 12px 16px; background: #0d0d10; border-top: 1px solid #27272a; }
.vkey-nodata { padding: 12px 16px; background: #0d0d10; border-top: 1px solid #27272a; font-size: 0.8rem; color: #52525b; font-style: italic; }
.usage-table { width: 100%; border-collapse: collapse; font-size: 0.78rem; }
.usage-table th { text-align: left; padding: 4px 8px 6px; color: #52525b; font-weight: 600; font-size: 0.7rem; text-transform: uppercase; letter-spacing: .05em; border-bottom: 1px solid #1f1f23; }
.usage-table td { padding: 6px 8px; border-bottom: 1px solid #1a1a1d; color: #a1a1aa; }
.usage-table tr:last-child td { border-bottom: none; }
.usage-table td.model-cell { color: #7c3aed; font-family: monospace; font-size: 0.76rem; }
.usage-table td.num { text-align: right; font-variant-numeric: tabular-nums; color: #d4d4d8; }

/* ── Feedback ── */
.feedback { margin-top: 14px; padding: 9px 14px; border-radius: 8px; font-size: 0.82rem; display: none; }
.feedback.ok  { background: #052e16; color: #4ade80; border: 1px solid #14532d; display: block; }
.feedback.err { background: #450a0a; color: #f87171; border: 1px solid #7f1d1d; display: block; }

/* ── Prometheus link ── */
.metrics-link { font-size: 0.8rem; color: #52525b; text-align: center; }
.metrics-link a { color: #7c3aed; text-decoration: none; }
.metrics-link a:hover { text-decoration: underline; }
</style>
</head>
<body>
<div class="page">

  <!-- header -->
  <div class="card" style="padding:20px 32px">
    <div class="logo">Claude Proxy</div>
    <div class="sub">Admin dashboard · auto-refreshes every 5 s</div>
  </div>

  <!-- OAuth tokens -->
  <div class="card">
    <div class="section-title">Upstream OAuth Tokens</div>
    <div id="token-list"></div>
    <div class="feedback" id="fb"></div>
  </div>

  <!-- Virtual keys usage -->
  <div class="card">
    <div class="section-title">Virtual API Keys — Token Usage</div>
    <div id="vkey-list"></div>
  </div>

  <div class="metrics-link">Prometheus metrics available at <a href="/metrics" target="_blank">/metrics</a></div>
</div>
<script>
let state = { tokens: [], active: "", headers: {}, virtual_keys: [] };

async function init() {
  await refresh();
  setInterval(refresh, 5000);
}

async function refresh() {
  try {
    const r = await fetch("/state");
    state = await r.json();
    render();
  } catch (e) { /* ignore transient errors */ }
}

/* ── Formatting helpers ── */
function fmtTokens(n) {
  n = n || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(n);
}

function fmtReset(ts) {
  if (!ts) return "";
  const secs = Math.max(0, parseInt(ts) - Date.now() / 1000);
  if (secs < 60) return "< 1 min";
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60);
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

/* ── OAuth token rendering ── */
function usageBar(h, period) {
  const util  = h[`anthropic-ratelimit-unified-${period}-utilization`];
  const reset = h[`anthropic-ratelimit-unified-${period}-reset`];
  const status = h[`anthropic-ratelimit-unified-${period}-status`];
  if (util === undefined) return "";
  const pct = Math.round(parseFloat(util) * 100);
  const color = barColor(util);
  return `
    <div class="usage-row">
      <div class="usage-label-row">
        <span class="period-label">${period}</span>
        <span class="pct-label" style="color:${color}">${pct}%</span>
        <span class="badge-status ${statusClass(status)}">${status || ""}</span>
        <span class="reset-label">resets in ${fmtReset(reset)}</span>
      </div>
      <div class="bar-track"><div class="bar-fill" style="width:${pct}%;background:${color}"></div></div>
    </div>`;
}

function renderRawHeaders(h) {
  if (!h || Object.keys(h).length === 0) return "";
  const skip = new Set([
    "anthropic-ratelimit-unified-5h-utilization","anthropic-ratelimit-unified-7d-utilization",
    "anthropic-ratelimit-unified-5h-reset","anthropic-ratelimit-unified-7d-reset",
    "anthropic-ratelimit-unified-5h-status","anthropic-ratelimit-unified-7d-status",
  ]);
  const metaKeys = [
    "anthropic-ratelimit-unified-status","anthropic-ratelimit-unified-representative-claim",
    "anthropic-ratelimit-unified-fallback","anthropic-ratelimit-unified-fallback-percentage",
    "anthropic-ratelimit-unified-overage-status","anthropic-ratelimit-unified-overage-disabled-reason",
  ];
  const metaItems = metaKeys.filter(k => h[k] !== undefined).map(k => {
    const shortKey = k.replace("anthropic-ratelimit-unified-", "");
    const cls = metaValClass(k, h[k]);
    return `<span class="meta-item"><span class="meta-key">${shortKey}:</span> <span class="meta-val ${cls}">${h[k]}</span></span>`;
  }).join("");
  const rows = Object.entries(h).sort(([a],[b]) => a.localeCompare(b))
    .map(([k,v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("");
  return `
    ${metaItems ? `<div class="meta-row">${metaItems}</div>` : ""}
    <details>
      <summary>Raw headers (${Object.keys(h).length})</summary>
      <table class="raw-table">${rows}</table>
    </details>`;
}

function renderOAuthTokens() {
  document.getElementById("token-list").innerHTML = state.tokens.map(n => {
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
        ${renderRawHeaders(h)}
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

/* ── Virtual key rendering ── */
function renderVirtualKeys() {
  const vkeys = state.virtual_keys || [];
  document.getElementById("vkey-list").innerHTML = vkeys.map(vk => {
    const usage = vk.usage || {};
    const models = Object.keys(usage).sort();
    const totalReq = models.reduce((s, m) => s + (usage[m].requests || 0), 0);
    const totalIn  = models.reduce((s, m) => s + (usage[m].input_tokens || 0), 0);
    const totalOut = models.reduce((s, m) => s + (usage[m].output_tokens || 0), 0);

    const modelRows = models.map(m => `
      <tr>
        <td class="model-cell">${m}</td>
        <td class="num">${usage[m].requests || 0}</td>
        <td class="num">${fmtTokens(usage[m].input_tokens)}</td>
        <td class="num">${fmtTokens(usage[m].output_tokens)}</td>
      </tr>`).join("");

    return `
      <div class="vkey-card">
        <div class="vkey-header">
          <span class="vkey-name">${vk.name}</span>
          <div class="vkey-totals">
            <div class="vkey-stat">
              <span class="vkey-stat-val">${totalReq}</span>
              <span class="vkey-stat-lbl">requests</span>
            </div>
            <div class="vkey-stat">
              <span class="vkey-stat-val">${fmtTokens(totalIn)}</span>
              <span class="vkey-stat-lbl">input</span>
            </div>
            <div class="vkey-stat">
              <span class="vkey-stat-val">${fmtTokens(totalOut)}</span>
              <span class="vkey-stat-lbl">output</span>
            </div>
          </div>
        </div>
        ${models.length > 0 ? `
        <div class="vkey-body">
          <table class="usage-table">
            <thead><tr><th>Model</th><th>Requests</th><th>Input tokens</th><th>Output tokens</th></tr></thead>
            <tbody>${modelRows}</tbody>
          </table>
        </div>` : `<div class="vkey-nodata">No usage recorded yet</div>`}
      </div>`;
  }).join("");
}

function render() {
  renderOAuthTokens();
  renderVirtualKeys();
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
        "virtual_keys": [
            {"name": name, "usage": _usage_stats.get(name, {})}
            for name in VIRTUAL_KEYS
        ],
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


@admin_app.get("/metrics")
async def admin_metrics():
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Entry point — run proxy + admin servers concurrently
# ---------------------------------------------------------------------------

async def _main():
    admin_port = int(os.environ.get("ADMIN_PORT", "8090"))
    proxy_cfg = uvicorn.Config(app, host="0.0.0.0", port=8080, log_config=None)
    admin_cfg = uvicorn.Config(admin_app, host="0.0.0.0", port=admin_port, log_config=None)
    proxy_srv = uvicorn.Server(proxy_cfg)
    admin_srv = uvicorn.Server(admin_cfg)
    admin_srv.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    await asyncio.gather(proxy_srv.serve(), admin_srv.serve())


if __name__ == "__main__":
    asyncio.run(_main())
