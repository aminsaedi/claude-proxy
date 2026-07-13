# CLAUDE.md

## Project overview

`claude-proxy` is a self-hosted Anthropic API proxy: clients authenticate with
**virtual keys**, the proxy forwards to `api.anthropic.com` using subscription
**OAuth tokens**, with per-request failover, usage tracking, health probing, and
auto-rotation. It runs two FastAPI apps concurrently — a proxy on port 8080 and
an admin UI on port 8090 — plus background tasks, all under one asyncio loop.

## Module map (`src/claude_proxy/`)

- `app.py` — `AppState` (all shared state, no globals) + `main()` that starts both
  uvicorn servers and the background tasks (usage flush, health, rotation, vkey reload).
- `proxy_app.py` — the `/v1/{path}` passthrough with failover + usage capture; `/healthz`.
- `admin_app.py` — dashboard + `/state` `/select` `/probe` `/config` `/metrics`; HTTP Basic auth.
- `stores.py` — `TokenStore` (health incl. rate-limited vs unhealthy, failover order) and
  `VirtualKeyStore` (mtime hot-reload, `resolve()`).
- `usage.py` — `UsageTracker`: async-locked, debounced, atomic; tracks cache tokens.
- `health.py` — token probing (429 → rate_limited, 401/403 → unhealthy) + `health_loop`.
- `rotation.py` — health-aware auto-rotation (`rotation_loop`).
- `config.py` / `models.py` — pydantic `AppConfig` load/save (atomic) with validation.
- `metrics.py` — Prometheus. `atomicio.py` — temp-file+rename writes. `paths.py` — data-dir resolution.
- `static/admin.html` — the dashboard (extracted from Python).
- `manage.py` (repo root) — rich TUI, talks to the admin API + edits YAML.

## Data files (bind-mounted `data/`, all gitignored)

`data/tokens.yaml`, `data/virtual_keys.yaml`, `data/config.yaml`,
`data/usage_stats.json`. Path root is `CLAUDE_PROXY_DATA_DIR` (`/app/data` in the
container). A single directory is mounted (not individual files) so atomic
temp+rename writes work.

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

## What NOT to do

- Do not commit anything under `data/` or `.env` — secrets.
- Do not expose the admin port (8182) publicly; keep it Tailscale-only and set `ADMIN_PASSWORD`.
- Do not add per-file bind mounts for the data files — atomic writes need the `data/` dir mounted.
- Do not revert usage tracking to input/output only — cache tokens dominate real spend.
