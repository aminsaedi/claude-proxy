# CLAUDE.md

## Project overview

`claude-proxy` is a self-hosted Anthropic API proxy: clients authenticate with
**virtual keys**, the proxy forwards to `api.anthropic.com` using subscription
**OAuth tokens**, with per-request failover, usage + cost tracking, per-key spend
limits, health probing, and auto-rotation. It runs two FastAPI apps concurrently
— a proxy on port 8080 and an admin UI on port 8090 — plus background tasks, all
under one asyncio loop.

## Module map (`src/claude_proxy/`)

- `app.py` — `AppState` (all shared state, no globals) + `main()` that starts both
  uvicorn servers and the background tasks (usage flush, health, rotation, vkey reload).
- `proxy_app.py` — the `/v1/{path}` passthrough with failover + usage capture; `/healthz`.
- `admin_app.py` — dashboard + `/state` `/select` `/probe` `/config` `/metrics`; HTTP Basic auth.
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
- `static/admin.html` — the dashboard (extracted from Python).
- `manage.py` (repo root) — rich TUI; CRUD via `db`, live actions via the admin API.

## Storage (SQLite)

Everything lives in one SQLite DB, `CLAUDE_PROXY_DB` (default
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
