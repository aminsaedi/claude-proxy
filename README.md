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
the module map). It runs as a **non-root** user (uid 10001).

## Storage

All state — tokens, virtual keys, config, and usage — lives in a single
**SQLite** database (`data/claude_proxy.db`, WAL mode). The app serves from
in-memory caches and only touches the DB at startup, on the debounced usage
flush, and on admin/CRUD calls, so the request hot path never blocks on I/O.

| Path | Purpose | Gitignored |
|------|---------|------------|
| `data/claude_proxy.db` | SQLite: tokens, keys, config, usage | Yes |
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

## Deploy on k3s

Manifests live in `k8s/claude-proxy.yaml`. The deployment (see `CLAUDE.md`):

- **API → internet** via a Traefik `Ingress` (`claude-proxy.aminsaedi.com`,
  wildcard TLS from cert-manager) fronted by the in-cluster Cloudflare Tunnel.
- **Admin → tailnet only** via a `Service type: LoadBalancer` +
  `loadBalancerClass: tailscale` (L4), reachable at
  `claude-proxy-admin.<tailnet>.ts.net:8090`.
- SQLite DB on a `local-path` PVC (pod pinned to the node that owns it); runs
  non-root (uid 10001, `fsGroup`); image from the in-cluster registry.

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
