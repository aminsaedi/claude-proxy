# claude-proxy

A self-hosted proxy for the Anthropic Claude API. It sits between your clients
and `api.anthropic.com`, authenticating upstream with subscription **OAuth
tokens** while exposing simple **virtual API keys** to your clients. Multiple
upstream tokens can be configured; the proxy transparently **fails over** on
rate-limits/errors, tracks per-key usage **and dollar cost** (including cache
tokens), enforces per-key **spend limits**, **records every request and prompt**
for auditing, and can **auto-rotate** the active token from a Tailscale-only
admin console.

## Architecture

```
clients (virtual API key)
        │
        ▼
  proxy :8080 ──OAuth──►  api.anthropic.com
        │   └─ per-request failover across tokens on 429/5xx
  admin :8090  (Tailscale only, HTTP Basic auth)
```

- **Proxy** (`8080 → host 8181`): accepts `x-api-key` or `Authorization: Bearer`
  with a virtual key, forwards to Anthropic with the active OAuth token. Streams
  SSE. On a retryable upstream error it retries the request on the next healthy
  token before the client ever sees a failure.
- **Admin console** (`8090 → host 8182`, Tailscale-only): five views —
  Overview (capacity gauge, fleet spend, latency percentiles, cost by model),
  Requests (the live audit log with a prompt/response drill-down), Clients
  (per-key usage, spend windows, limits), Upstreams (token health and rotation),
  and Settings. Protected by HTTP Basic auth when `ADMIN_PASSWORD` is set.

The app is a typed Python package under `src/claude_proxy/` (see `CLAUDE.md` for
the module map). It runs as a **non-root** user (uid 10001).

## Storage

All state — tokens, virtual keys, config, and usage — lives in a single
**SQLite** database (`data/claude_proxy.db`, WAL mode). The app serves from
in-memory caches and only touches the DB at startup, on the debounced usage
flush, and on admin/CRUD calls, so the request hot path never blocks on I/O.

| Path | Purpose | Gitignored |
|------|---------|------------|
| `data/claude_proxy.db` | SQLite: tokens, keys, config, usage, spend limits | Yes |
| `data/audit.db` | SQLite: the request/prompt audit log (own file, own budget) | Yes |
| `data/model_prices.json` | Cached copy of the online model price list | Yes |
| `.env` | `TAILSCALE_IP`, `ADMIN_USER`, `ADMIN_PASSWORD` | Yes |

Manage tokens/keys/settings with the TUI (`manage.py`) or the admin UI. There is
no YAML to hand-edit. Migrating from the old YAML layout:
`python -m claude_proxy.migrate` imports `tokens.yaml` / `virtual_keys.yaml` /
`config.yaml` / `usage_stats.json` from the data dir into the DB.

## Setup (Docker Compose)

```bash
cp .env.example .env          # set TAILSCALE_IP + ADMIN_PASSWORD
./install.sh                  # prompts for a token + key, seeds the DB, starts
```

`install.sh` seeds `data/claude_proxy.db`, `chown`s `data/` to uid 10001, and
runs `docker compose up -d --build`. Add more tokens/keys later via `manage.py`.

## Usage

```bash
ANTHROPIC_BASE_URL=http://localhost:8181 \
ANTHROPIC_API_KEY=vk-alice-secret-key \
  claude ...
```

## Admin console

Open `http://<tailscale-ip>:8182` (Basic auth). Five views:

| View | What it shows |
|------|----------------|
| **Overview** | Active-token 5h capacity gauge, fleet spend tiles, daily spend chart, TTFB/total latency percentiles, cost and volume by model |
| **Requests** | The live audit log — search, filter by client/outcome/window, click any row for the full prompt, tool definitions, and completion |
| **Clients** | Per-key spend windows, a daily chart, spend limits, per-model breakdown, and key rotate/rename/reveal |
| **Upstreams** | Per-token 5h/7d utilization, health, live switch, probe, and the rotation log |
| **Settings** | Auto-rotation, auditing, probes, timezone, pricing, and the audit storage meter |

Dashboard state streams over Server-Sent Events and only re-renders when
something actually changed, so scroll position, open panels, and half-typed
forms survive a live feed. The request log polls separately, and only while its
tab is on screen. Dark and light themes are both first-class — each palette was
validated for colourblind separation and contrast against its own surface,
rather than one being flipped from the other.

## Cost tracking

Every response's token counts are priced the moment they're recorded, using
per-token rates from LiteLLM's open
[`model_prices_and_context_window.json`](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json).
The table is fetched at startup and every `pricing.refresh_hours`, cached to
`data/model_prices.json` so a restart without network still costs correctly, and
backed by a small built-in fallback. Because cost is stored per request, a later
price change never rewrites past spend. Cache-read and cache-write tokens are
priced at their own rates, and prompts over 200k tokens use the long-context
tier. A model with no published price is counted as `$0.00` and called out in
the console rather than guessed at.

Spend is bucketed per hour and rolled up into **1d / 3d / 7d / 30d** views plus a
14-day daily bar chart, per virtual key and per model — all in calendar days for
the configured timezone (`America/Toronto` by default).

## Spend limits

Each virtual key can carry caps for several periods **at the same time** — e.g.
`$3/hour + $10/day`. Every cap is checked before a request is forwarded; while
any one is exceeded the proxy replies `429` with Anthropic's error envelope and
a `Retry-After` pointing at the window rollover, and never calls upstream:

```json
{"type":"error","error":{"type":"rate_limit_error",
 "message":"Spend limit reached: $10.42 of $10.00 per day. Resets in 5h 51m."}}
```

Windows are calendar-aligned in the configured timezone — the hourly cap resets
on the hour, daily at local midnight, weekly on Monday, monthly on the 1st — so
a reset happens at a time you can predict. Set them in the admin UI ("Set limits"
on a client), via `manage.py → Spend Limits`, or over the API:

```bash
curl -X PUT http://<admin>/virtual-keys/laptop/limits \
  -H 'content-type: application/json' -d '{"limits":{"hour":3,"day":10}}'
```

The payload replaces the key's whole set of caps; omit a period to remove it and
send `{"limits":{}}` to uncap entirely. Spend is booked when a response
completes, so requests already in flight can overshoot a cap slightly — these
are budget guards, not a transactional ledger.

## Request & prompt auditing

Every proxied request can be recorded in full: the prompt that went up, the
completion that came back, who sent it, which upstream token served it, the
token counts, the cost, the time to first byte, and the total duration. The
Requests view lists them live and opens any one of them to show the system
prompt, tool definitions, and every turn.

**It does not cost latency.** The request handler builds a small record and
drops it on a bounded in-memory queue — measured at ~10µs, against requests that
take hundreds of milliseconds. JSON encoding, compression, and the SQLite insert
all happen on a dedicated writer thread. If that thread ever falls behind, the
queue drops records and increments a counter rather than making a live request
wait; losing an audit row is always better than slowing down the thing being
audited. (The same release made streaming *faster*: the SSE scan now dispatches
on the `event:` name instead of parsing every frame, 8.1µs → 1.0µs per event.)

Three modes, set under Settings:

| Mode | What it stores |
|------|----------------|
| `off` | nothing |
| `meta` | one row per request — who, what, cost, latency — but no prompt text |
| `full` | the above plus the prompt and completion, compressed (default) |

Retention is enforced on **both** axes, whichever bites first: records older
than `retention_days` (default 7) are dropped, and if the file still exceeds
`max_gb` (default 2) the oldest records go until it fits. Bodies over
`max_body_kb` (default 256) are truncated so one enormous context can't consume
the budget. In practice a recorded request costs ~750 bytes compressed, so 2GB
holds on the order of a million of them. The database is created with
`auto_vacuum=INCREMENTAL` and checkpointed after each sweep, so deleted space is
genuinely returned to the filesystem rather than left in the file.

The audit log lives in its own SQLite file (`data/audit.db`) so its write volume
never contends with the tokens/keys/usage tables.

## Prometheus metrics

At `http://<tailscale-ip>:8182/metrics` (no auth, for scraping):
`proxy_requests_total`, `proxy_{input,output,cache_read,cache_creation}_tokens_total`,
`proxy_cost_usd_total`, `proxy_key_window_{spend,limit}_usd`,
`proxy_limit_blocks_total`, `proxy_upstream_utilization_{5h,7d}_ratio`,
`proxy_token_healthy`, `proxy_auto_rotations_total`, `proxy_failovers_total`,
`proxy_request_latency_seconds`, `proxy_upstream_ttfb_seconds`,
`proxy_audit_{queue_depth,records_written,records_dropped,db_bytes,rows}`.

## Managing keys/tokens (TUI)

```bash
docker compose exec -it proxy python manage.py
```

## Deploy on k3s

Manifests live in `k8s/claude-proxy.yaml`. The deployment (see `CLAUDE.md`):

- **API → internet** via a Traefik `Ingress` (`claude-proxy.aminsaedi.com`,
  wildcard TLS from cert-manager) fronted by the in-cluster Cloudflare Tunnel.
- **Admin → tailnet only** via a `Service type: LoadBalancer` +
  `loadBalancerClass: tailscale` (L4), reachable at
  `claude-proxy-admin.<tailnet>.ts.net:8090`.
- SQLite DB on a `local-path` PVC (pod pinned to the node that owns it); runs
  non-root (uid 10001, `fsGroup`); image from the in-cluster registry.

### Zero-downtime rollouts

The deployment uses `RollingUpdate` with `maxSurge: 1, maxUnavailable: 0`, so
the old pod is only torn down once the new one is ready. Four things make that
safe rather than merely optimistic:

- **Both pods share the PVC.** They are pinned to the same node and
  `ReadWriteOnce` is node-scoped, not pod-scoped, so both can mount it.
- **Usage writes are additive.** Flushes apply deltas (`x = x + excluded.x`)
  and read the result back, so two processes writing the same SQLite file sum
  their traffic instead of overwriting each other. A replace-from-snapshot
  write — what this used to do — would silently lose whichever pod flushed
  first.
- **Readiness and liveness are different endpoints.** `/healthz` returns 503 the
  moment a drain starts, which is what removes the pod from the ingress;
  `/livez` keeps returning 200 so the kubelet doesn't kill the pod in the middle
  of the drain.
- **In-flight requests are allowed to finish.** A `preStop` sleep holds off
  SIGTERM while endpoint removal propagates, then the process fails readiness,
  waits, and only then winds both servers down — with a 120s grace period,
  because a streaming completion can legitimately run for minutes.

Rehearsed end to end before shipping: with a 6.6-second stream in flight,
SIGTERM flipped readiness to 503, left liveness green, let the stream run to
`[DONE]`, and exited 0 without a SIGKILL.

```bash
# build + push
docker build -t <registry>/claude-proxy:v2 . && docker push <registry>/claude-proxy:v2
# admin creds + one-time DB seed (kept out of git)
kubectl -n claude-proxy create secret generic claude-proxy-admin \
  --from-literal=ADMIN_USER=admin --from-literal=ADMIN_PASSWORD=<pw>
kubectl -n claude-proxy create secret generic claude-proxy-seed \
  --from-file=claude_proxy.db=<seeded db>     # delete after first deploy
kubectl apply -f k8s/claude-proxy.yaml
```

Add the public host to the Cloudflare Tunnel config
(`service: https://traefik.kube-system.svc.cluster.local:443`, `noTLSVerify: true`).

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
CLAUDE_PROXY_DATA_DIR=./data python -m claude_proxy   # DB at ./data/claude_proxy.db
pytest            # tests
ruff check .      # lint
mypy              # type-check
```
