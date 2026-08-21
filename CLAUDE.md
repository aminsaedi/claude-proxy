# CLAUDE.md

## Project overview

`claude-proxy` is a self-hosted Anthropic API proxy: clients authenticate with
**virtual keys**, the proxy forwards to `api.anthropic.com` using subscription
**OAuth tokens**, with per-request failover, usage + cost tracking, per-key spend
limits, a full request/prompt audit log, health probing, and auto-rotation. It
runs two FastAPI apps concurrently — a proxy on port 8080 and an admin console on
port 8090 — plus background tasks, all under one asyncio loop.

## Module map (`src/claude_proxy/`)

- `app.py` — `AppState` (all shared state, no globals) + `main()` that starts both
  uvicorn servers and the background tasks (usage flush, health, rotation, vkey reload).
- `proxy_app.py` — the `/v1/{path}` passthrough with failover + usage capture +
  audit capture; `/healthz` (readiness) and `/livez` (liveness).
- `audit.py` — `AuditLog`: bounded queue → writer thread → its own SQLite file.
  Age *and* size retention, keyset paging, latency percentiles.
- `admin_app.py` — console + `/state` `/events` `/requests` `/audit/*` `/select`
  `/probe` `/config` `/metrics`; HTTP Basic auth.
- `stores.py` — `TokenStore` (health incl. rate-limited vs unhealthy, failover order) and
  `VirtualKeyStore` (mtime hot-reload, `resolve()`).
- `usage.py` — `UsageTracker`: async-locked, debounced, atomic; lifetime totals **and**
  an hourly time series; tracks cache tokens and per-request cost.
- `pricing.py` — `PricingTable`: per-token USD rates from LiteLLM's open price list,
  disk-cached with a bundled fallback; model-name resolution + `refresh_loop`.
- `budgets.py` — tz-aware calendar `window_bounds`, `LimitStatus`/`evaluate`, `LimitStore`.
- `health.py` — token probing (429 → rate_limited, 401/403 → unhealthy) + `health_loop`.
- `rotation.py` — health-aware auto-rotation (`rotation_loop`).
- `db.py` — SQLite layer (schema + CRUD); the single source of truth.
- `config.py` / `models.py` — pydantic `AppConfig`, persisted as a JSON row in the DB.
- `migrate.py` — one-time YAML/JSON → SQLite importer (`python -m claude_proxy.migrate`).
- `metrics.py` — Prometheus. `paths.py` — data-dir / DB-path resolution.
- `static/console-dom.js` — the console's rendering primitives: `el` builds DOM,
  `txt`/`cls`/`sty`/`att`/`val` write a single value **only when it changed**,
  `list` reconciles a keyed collection by reusing nodes. Views build their DOM
  once and patch it; nothing re-renders markup on a live update.
- `static/console.{html,css,js}` — the admin console, served from `/` and
  `/assets/{name}` (an explicit allow-map, not a directory listing). The shell is
  `no-store` and stamps `__BUILD__` with the version into the asset URLs, so the
  assets can be cached hard and still turn over on deploy.
- `manage.py` (repo root) — rich TUI; CRUD via `db`, live actions via the admin API.

## Storage (SQLite)

Two SQLite files, deliberately separate. The audit log
(`CLAUDE_PROXY_AUDIT_DB`, default `$DATA_DIR/audit.db`) is on its own because its
write volume dwarfs everything else's; isolating it keeps that traffic off the
tokens/keys/usage tables. Everything else lives in `CLAUDE_PROXY_DB` (default
`$CLAUDE_PROXY_DATA_DIR/claude_proxy.db`; `/data/claude_proxy.db` in k8s). Tables:
`tokens`, `virtual_keys`, `config` (single JSON row), `usage` (lifetime totals),
`usage_hourly` (time series, one row per UTC hour × key × model), `key_limits`.
Schema changes are additive — `db._ADDED_COLUMNS` is applied on startup. WAL mode
lets the app and a `manage.py` process share the file. The request path never hits
the DB — tokens/keys/config/limits are cached in memory (vkeys and limits refreshed
from the DB every ~5s), usage is flushed on a debounced background task via
`asyncio.to_thread`.

## Deployment (k3s)

Manifests in `k8s/claude-proxy.yaml`. API is public via Traefik Ingress
(`claude-proxy.aminsaedi.com`) behind the in-cluster Cloudflare Tunnel; admin is
tailnet-only via a Tailscale L4 `LoadBalancer` service. DB on a `local-path` PVC
(pod pinned to its node), seeded once from a `claude-proxy-seed` Secret via an
initContainer, non-root uid 10001. Also runnable via `docker compose` (see README).

**Rollouts are zero-downtime** (`RollingUpdate`, `maxSurge: 1`,
`maxUnavailable: 0`). Four things make the overlap safe, and each has a
counterpart in the code — change one and re-read the other:

1. Both pods sit on the same node (`nodeSelector`), and RWO is node-scoped, so
   they can share the PVC.
2. `db.add_usage` / `db.add_usage_hourly` write **deltas**, not snapshots, so two
   live writers sum rather than clobber. Never reintroduce a replace-all flush.
3. `/healthz` (readiness) 503s while draining; `/livez` (liveness) does not.
   Pointing liveness at `/healthz` makes the kubelet kill the pod mid-drain.
4. `_disarm_uvicorn_signals` stops each uvicorn server installing its own SIGTERM
   handler — with two servers in one process, uvicorn's handler stops only one of
   them and the pod hangs until SIGKILL. Shutdown lives in `_install_shutdown`.

## Common tasks

- Run locally: `pip install -e ".[dev]"` then `CLAUDE_PROXY_DATA_DIR=./data python -m claude_proxy`
- Tests / lint / types: `pytest` · `ruff check .` · `mypy`
- Rebuild container: `docker compose up -d --build`
- Logs: `docker compose logs -f`
- TUI: `docker compose exec -it proxy python manage.py`

## Architecture notes

- **Auth flow**: client virtual key → `VirtualKeyStore.resolve` → active OAuth token
  as `Authorization: Bearer`, inject `anthropic-beta: oauth-2025-04-20`, forward.
- **Failover**: on 429/5xx the request is retried on the next healthy token
  (`TokenStore.failover_order`) before the client sees an error. 429 marks a token
  *rate_limited* (recoverable), 401/403 marks it *unhealthy* (dead) — these are distinct.
- **Rotation**: switches the active token on high 5h utilization OR when it's
  unusable; an unusable active token fails over regardless of `target_max_util_5h`.
- **Usage**: parsed from SSE (`message_start` for input+cache, `message_delta` for
  output) or the JSON `usage` block; flushed to disk on a debounced background task.
- **Cost**: priced at ingest from `PricingTable` and stored with the tokens, so a
  price change never rewrites history. Unknown models cost $0 and are surfaced in
  the console rather than guessed at.
- **Auditing**: `AuditLog.submit()` is the only thing the request path calls —
  it builds no JSON, compresses nothing, opens no file, and drops the record on
  a bounded queue (~10µs). Serialization, compression, and the insert happen on
  a writer thread. A full queue **drops** rather than applying backpressure. The
  SSE scan dispatches on the `event:` line name rather than parsing every frame.
- **Audit retention**: age *and* size, whichever bites first. `auto_vacuum=
  INCREMENTAL` must be set before the first write (before `journal_mode=WAL`),
  and `PRAGMA incremental_vacuum` must have its cursor drained — `execute()`
  alone frees exactly one page.
- **Spend limits**: several periods per key at once (`hour`/`day`/`week`/`month`),
  all enforced. Windows are calendar-aligned in `config.timezone` (default
  `America/Toronto`); hourly buckets line up with those edges for whole-hour zones.
  `AppState.limit_breach()` is checked *before* forwarding and reads only memory.
  Spend is booked after a response completes, so in-flight requests can overshoot.

## What NOT to do

- Do not commit anything under `data/` or `.env` — secrets.
- Do not expose the admin port (8182) publicly; keep it Tailscale-only and set `ADMIN_PASSWORD`.
- Do not add per-file bind mounts for the data files — atomic writes need the `data/` dir mounted.
- Do not revert usage tracking to input/output only — cache tokens dominate real spend.
- Do not make the usage flush replace-all again, and do not point the liveness
  probe at `/healthz` — both quietly break zero-downtime rollouts.
- Do not do audit work on the event loop (no compression, no JSON encoding, no
  DB access in the handler); the whole design is that the writer thread owns it.
- Do not reintroduce innerHTML-per-frame rendering in the console. It destroys
  focus and selection mid-edit, restarts transitions, and — because rendered
  strings contain countdowns — rebuilds cards every minute for no reason.
- Do not have a store's `reload_if_changed` blindly adopt what it read: a local
  write can commit inside that read window. `LimitStore` guards with a
  generation counter, `VirtualKeyStore` by holding read-and-adopt under one
  lock. Without it a save is silently rolled back — in the UI *and* in
  enforcement — until some later tick notices.
- Do not run a second proxy process against the same data dir except during a
  deliberate rollout overlap — it is safe now, but it doubles health probes,
  which cost real subscription quota.
