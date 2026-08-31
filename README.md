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
        │   └─ per-request failover across tokens on 429/500/502/503/529
  admin :8090  (Tailscale only)
```

- **Proxy** (container port `8080`): accepts `x-api-key` or
  `Authorization: Bearer` with a virtual key, forwards to Anthropic with the
  active OAuth token. Streams SSE. On a retryable upstream status — `429`,
  `500`, `502`, `503`, `529` — or a transport error, it retries on the next
  token in the failover order before the client sees a failure, trying at most
  3 tokens per request. Note that `504` is *not* retried.
- **Admin console** (container port `8090`): five views — Overview (capacity
  gauge, fleet spend, latency percentiles, cost by model), Requests (the live
  audit log with a prompt/response drill-down), Clients (per-key usage, spend
  windows, limits), Upstreams (token health and rotation), and Settings.
  HTTP Basic auth is applied **only when `ADMIN_PASSWORD` is set**; with it
  unset the console and its API are open to anyone who can reach the port,
  which is why the port is never exposed beyond a tailnet. (The k3s deployment
  in `k8s/` runs it without auth on purpose — see below.)

The k3s deployment uses these container ports directly. Both apps run in one
process, on one asyncio loop, as a **non-root** user (uid 10001).

The app is a typed Python package under `src/claude_proxy/` (see `CLAUDE.md` for
the module map).

## Storage

State lives in **two** SQLite files, both under the data dir
(`CLAUDE_PROXY_DATA_DIR`), both in WAL mode. The app serves from in-memory
caches and only touches the DB at startup, on the debounced usage flush, on the
5-second virtual-key/limit refresh, and on admin calls — the request hot path
never blocks on I/O.

| Path | Purpose | Gitignored |
|------|---------|------------|
| `data/claude_proxy.db` | SQLite: tokens, keys, config, usage, spend limits | Yes |
| `data/audit.db` | SQLite: the request/prompt audit log (own file, own budget) | Yes |
| `data/model_prices.json` | Cached copy of the online model price list | Yes |

Each `.db` has `-wal` and `-shm` siblings that SQLite creates next to it, so the
**directory** has to be writable and mounted — bind-mounting individual files
does not work.

Paths are overridable with `CLAUDE_PROXY_DB`, `CLAUDE_PROXY_AUDIT_DB`, and
`CLAUDE_PROXY_PRICING_CACHE`; ports with `PROXY_PORT` / `ADMIN_PORT`.

Manage tokens/keys/settings with the TUI (`manage.py`) or the admin UI. Nothing
reads YAML at runtime — the `*.yaml.example` files are the format of the
**one-time importer** only: `python -m claude_proxy.migrate` reads
`tokens.yaml` / `virtual_keys.yaml` / `config.yaml` / `usage_stats.json` from
the data dir into the DB, which is how an existing install is seeded.

## Setup

k3s is the only deployment target — see **Deploy on k3s** below. To run it
locally instead:

```bash
pip install -e ".[dev]"
CLAUDE_PROXY_DATA_DIR=./data python -m claude_proxy
```

Add tokens and keys via `manage.py` or the console — not by editing the YAML,
which is only ever read by `claude_proxy.migrate` and never again.

## Usage

```bash
ANTHROPIC_BASE_URL=https://claude-proxy.aminsaedi.com \
ANTHROPIC_API_KEY=vk-alice-secret-key \
  claude ...
```

## Admin console

Open `http://<tailscale-ip>:8090` or
`http://claude-proxy-admin.<tailnet>.ts.net:8090` (k3s). Five views:

| View | What it shows |
|------|----------------|
| **Overview** | Active-token 5h capacity gauge, fleet spend tiles, daily spend chart, TTFB/total latency percentiles, cost and volume by model |
| **Requests** | The live audit log — search, filter by client/outcome/window, open any row (click, or Enter/Space from the keyboard) for the full prompt, tool definitions, and completion |
| **Clients** | Split view: a searchable, sortable list beside a detail pane with spend windows, a 14-day daily chart, an inline spend-limit editor, and the per-model breakdown. Selecting a client deep-links (`#clients/<name>`) and never moves the layout |
| **Upstreams** | Per-token 5h/7d utilization, health, live switch, probe, and the rotation log |
| **Settings** | Auto-rotation, auditing, probes, timezone, pricing, and the audit storage meter |

Dashboard state streams over Server-Sent Events, and views **patch** the DOM
rather than rebuilding it — individual values are written only when they change,
and a field you are editing is never written to at all. That is what keeps
scroll position, selection, and half-typed forms intact under a live feed. The
request log polls separately (every 2.5s) and only while its tab is on screen
and the browser tab is visible.

The shell scrolls in exactly **one** place: the document itself does not scroll,
and `main#scroll` is the only scroll region, so the wheel always moves one
thing. Nothing inside it — the request table, the client list, a prompt turn —
is allowed to grow a vertical scrollbar of its own; long prompt turns clamp
behind a "Show all" toggle instead, and narrow viewports drop columns rather
than adding a horizontal scrollbar under the vertical one.

`scripts/ui-smoke.py` asserts all of that against a running instance in a real
browser: every view rendering in both themes without console errors, no nested
same-axis scrollers (at 1500px and 620px, and inside the open drawer), the
drawer taking and returning focus, and a spend-limit save showing up without a
refresh. Dark and light are independent palettes — each defines its own token
set rather than one being derived from the other.

## Cost tracking

Every response's token counts are priced the moment they're recorded, using
per-token rates from LiteLLM's open
[`model_prices_and_context_window.json`](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json).
The table is fetched at startup and every `pricing.refresh_hours` (default 12),
cached to `data/model_prices.json` so a restart without network still costs
correctly, and backed by a small built-in fallback. Because cost is stored per
request, a later price change never rewrites past spend. Cache-read and
cache-write tokens are priced at their own rates, and once a request's prompt
exceeds 200k tokens the whole request is priced at the long-context tier. A
model with no published price is counted as `$0.00` and called out in the
console rather than guessed at.

Spend is bucketed per hour and rolled up into **today / 3d / 7d / 30d** views
plus a 14-day daily bar chart, per virtual key and per model — all in calendar
days for the configured timezone (`America/Toronto` by default).

## Spend limits

Each virtual key can carry caps for several periods **at the same time** — e.g.
`$3/hour + $10/day`. Every cap is checked before a request is forwarded; while
any one is exceeded the proxy replies `429` with Anthropic's error envelope and
a `Retry-After` pointing at the window rollover, and never calls upstream:

```json
{"type":"error","error":{"type":"rate_limit_error",
 "message":"Spend limit reached: $10.42 of $10.00 per day. Resets in 5h 51m."}}
```

`Retry-After` is capped at 3600s, so a monthly cap doesn't hand a client a
three-week backoff.

Windows are calendar-aligned in the configured timezone — the hourly cap resets
on the hour, daily at local midnight, weekly on Monday, monthly on the 1st — so
a reset happens at a time you can predict. Set them in the admin UI (the Clients
detail pane), via `manage.py → Spend Limits`, or over the API:

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
drops it on a bounded in-memory queue (4096 records) — measured at **2.0µs p50 /
2.6µs p95** per `submit()`, against requests that take hundreds of milliseconds.
JSON encoding, compression, and the SQLite insert all happen on a dedicated
writer thread. If that thread ever falls behind, the queue drops records and
increments a counter rather than making a live request wait; losing an audit row
is always better than slowing down the thing being audited.

The SSE scan is kept cheap the same way: frames are dispatched on the `event:`
line rather than deserialized. In `meta` mode only the two usage-bearing events
are parsed, which is the difference between ~7.4µs and ~0.13µs for every other
frame in a long completion. In `full` mode `content_block_delta` is parsed too —
that text *is* the completion being recorded — so the saving there applies to
`ping`, `content_block_start/stop`, and `message_stop`.

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
the budget.

Budget for the real thing, not for a toy: measured on the production instance, a
fully-captured Claude Code request costs **~105KB on disk** (2,315 rows in
242MB, no free pages). Prompts are big — the average request body was 1.3MB
before the 256KB cap, and 256KB of JSON zlib-compresses to only ~103KB — so the
body cap, not the request count, sets the row size. That puts 2GB at roughly
**20,000 requests**. At this instance's 7-day average of ~1,160 requests/day
that is ~17 days, so the 7-day age cap governs; on a heavy day (~4,700
requests) 2GB is gone in ~4 days and the size cap governs instead. If sustained
volume climbs, lower `max_body_kb` rather than raising `max_gb` — it scales
almost linearly, and the head of a prompt is what identifies it anyway.

The database is created with `auto_vacuum=INCREMENTAL` and checkpointed after
each sweep, so deleted space is genuinely returned to the filesystem rather than
left in the file. It lives in its own SQLite file (`data/audit.db`) so its write
volume never contends with the tokens/keys/usage tables.

## Prometheus metrics

At `/metrics` on the **admin** port — `http://<tailscale-ip>:8090/metrics`.
This endpoint is deliberately exempt from Basic auth so a scraper doesn't need
credentials:

`proxy_requests_total`, `proxy_input_tokens_total`, `proxy_output_tokens_total`,
`proxy_cache_read_input_tokens_total`,
`proxy_cache_creation_input_tokens_total`, `proxy_cost_usd_total`,
`proxy_key_window_spend_usd`, `proxy_key_window_limit_usd`,
`proxy_limit_blocks_total`, `proxy_upstream_utilization_5h_ratio`,
`proxy_upstream_utilization_7d_ratio`, `proxy_token_healthy`,
`proxy_auto_rotations_total`, `proxy_failovers_total`,
`proxy_request_latency_seconds`, `proxy_upstream_ttfb_seconds`,
`proxy_audit_queue_depth`, `proxy_audit_records_written`,
`proxy_audit_records_dropped`, `proxy_audit_db_bytes`, `proxy_audit_rows`.

## Managing keys/tokens (TUI)

```bash
ssh amin@mx kubectl -n claude-proxy exec -it deploy/claude-proxy -- python manage.py
```

## Deploy on k3s

Manifests live in `k8s/claude-proxy.yaml`. The deployment (see `CLAUDE.md`):

- **API → internet** via a Traefik `Ingress` (`claude-proxy.aminsaedi.com`,
  wildcard TLS from cert-manager) fronted by the in-cluster Cloudflare Tunnel.
- **Admin → tailnet only** via a `Service type: LoadBalancer` +
  `loadBalancerClass: tailscale` (L4), reachable at
  `claude-proxy-admin.<tailnet>.ts.net:8090`. The pod sets no `ADMIN_PASSWORD`,
  so **the console runs unauthenticated** — the tailnet is the whole access
  control. Add `ADMIN_USER`/`ADMIN_PASSWORD` env (e.g. from a Secret) to turn
  Basic auth back on.
- SQLite DB on a `local-path` PVC (pod pinned to the node that owns it); runs
  non-root (uid 10001, `fsGroup`); image from the in-cluster registry. The PVC
  requests 1Gi, which `local-path` neither enforces nor can expand — the real
  ceiling is free space on the node, and the audit log polices itself against
  its own `max_gb`.

### Zero-downtime rollouts

The deployment uses `RollingUpdate` with `maxSurge: 1, maxUnavailable: 0` and
`minReadySeconds: 10`, so the old pod is only torn down once the new one has
been ready for a while. Four things make that safe rather than merely
optimistic:

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
- **In-flight requests are allowed to finish.** A `preStop` sleep (6s) holds off
  SIGTERM while endpoint removal propagates; the process then fails readiness,
  waits `PROXY_DRAIN_LEAD` (3s in the manifest, 5s by default), and only then
  winds both servers down — with a 120s grace period, because a streaming
  completion can legitimately run for minutes.

`scripts/rollout.sh` performs the rollout and *proves* the claim: it drives
continuous `/healthz` traffic through the public endpoint for the whole rollout
and counts every response, so a single refused request is a failure rather than
a blip nobody was watching for. The v3.2.0 rollout scored 99/99 at HTTP 200.

```bash
# build + push
docker build -t <registry>/claude-proxy:v13 . && docker push <registry>/claude-proxy:v13
# one-time DB seed, consumed by the initContainer (delete after first deploy)
kubectl -n claude-proxy create secret generic claude-proxy-seed \
  --from-file=claude_proxy.db=<seeded db>
# roll it out and verify no traffic was dropped
KUBECTL_SSH=amin@mx ./scripts/rollout.sh v13
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

# browser smoke test — run it against a scratch instance, not production:
# it writes a spend limit, and refuses to write at all to a non-loopback
# target unless you pass --allow-writes.
pip install playwright && playwright install chromium
python scripts/ui-smoke.py http://127.0.0.1:8090
```
