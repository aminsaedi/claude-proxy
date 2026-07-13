# claude-proxy

A self-hosted proxy for the Anthropic Claude API. It sits between your clients
and `api.anthropic.com`, authenticating upstream with subscription **OAuth
tokens** while exposing simple **virtual API keys** to your clients. Multiple
upstream tokens can be configured; the proxy transparently **fails over** on
rate-limits/errors, tracks per-key usage (including cache tokens), and can
**auto-rotate** the active token from a Tailscale-only admin UI.

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
- **Admin UI** (`8090 → host 8182`, Tailscale-only): token utilization/health,
  live token switch, per-virtual-key usage (input/output **and cache** tokens),
  settings, and Prometheus metrics. Protected by HTTP Basic auth when
  `ADMIN_PASSWORD` is set.

The app is a typed Python package under `src/claude_proxy/` (see `CLAUDE.md` for
the module map). It runs as a **non-root** user in the container.

## Layout

Runtime data lives in a single bind-mounted `data/` directory (so writes are
atomic — temp file + rename):

| File | Purpose | Gitignored |
|------|---------|------------|
| `data/tokens.yaml` | Upstream OAuth tokens | Yes |
| `data/virtual_keys.yaml` | Client virtual keys | Yes |
| `data/config.yaml` | Hot-reloadable settings | Yes |
| `data/usage_stats.json` | Persisted usage counters | Yes |
| `.env` | `TAILSCALE_IP`, `ADMIN_USER`, `ADMIN_PASSWORD` | Yes |

> ⚠️ Everything under `data/` and `.env` is secret — all gitignored. Never commit them.

## Setup

```bash
cp .env.example .env                       # set TAILSCALE_IP + ADMIN_PASSWORD
mkdir -p data
cp tokens.yaml.example        data/tokens.yaml
cp virtual_keys.yaml.example  data/virtual_keys.yaml
cp config.yaml.example        data/config.yaml
echo '{}' > data/usage_stats.json
sudo chown -R 10001:10001 data             # container runs as uid 10001
docker compose up -d --build
```

Or run the guided installer: `./install.sh`.

**`data/tokens.yaml`**
```yaml
tokens:
  - name: personal
    token: "sk-ant-oat-..."
    default: true
  - name: work
    token: "sk-ant-oat-..."
```

**`data/virtual_keys.yaml`** (hot-reloaded within ~5s — no restart needed)
```yaml
virtual_keys:
  - name: alice
    key: "vk-alice-secret-key"
```

## Usage

```bash
ANTHROPIC_BASE_URL=http://localhost:8181 \
ANTHROPIC_API_KEY=vk-alice-secret-key \
  claude ...
```

## Admin UI

Open `http://<tailscale-ip>:8182` (Basic auth). Shows 5h/7d utilization, health
(healthy / rate-limited / unhealthy), live token switch, per-key usage with cache
columns, settings, and an auto-rotation log. Auto-refreshes every 5s.

## Prometheus metrics

At `http://<tailscale-ip>:8182/metrics` (no auth, for scraping):
`proxy_requests_total`, `proxy_{input,output,cache_read,cache_creation}_tokens_total`,
`proxy_upstream_utilization_{5h,7d}_ratio`, `proxy_token_healthy`,
`proxy_auto_rotations_total`, `proxy_failovers_total`, `proxy_request_latency_seconds`.

## Managing keys/tokens (TUI)

```bash
docker compose exec -it proxy python manage.py
```

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
CLAUDE_PROXY_DATA_DIR=./data python -m claude_proxy   # run locally
pytest            # tests
ruff check .      # lint
mypy              # type-check
```
