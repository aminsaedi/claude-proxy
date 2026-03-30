# claude-proxy

A lightweight self-hosted proxy for the Anthropic Claude API. It sits between
your clients and `api.anthropic.com`, authenticating upstream with OAuth tokens
while exposing simple API keys to your clients. Multiple upstream tokens can be
configured and switched live from an admin UI.

## Architecture

```
clients (virtual API key)
        │
        ▼
  proxy :8080  ──OAuth──►  api.anthropic.com
        │
  admin :8090  (Tailscale only)
```

- **Proxy** (`port 8080 → host 8181`): Accepts `x-api-key` or `Authorization: Bearer` with a virtual key, proxies to Anthropic with the active OAuth token. Supports streaming.
- **Admin UI** (`port 8090 → host 8182`): Shows OAuth token utilization/rate-limit headers, lets you switch the active token, and displays per-virtual-key usage. Bound to `$TAILSCALE_IP` only.
- **Prometheus metrics** at `/metrics` on the admin port.

## Setup

### 1. Upstream OAuth tokens — `tokens.yaml`

Copy the example and fill in your Anthropic OAuth tokens:

```bash
cp tokens.yaml.example tokens.yaml
```

```yaml
tokens:
  - name: personal
    token: "sk-ant-oat-..."
    default: true
  - name: work
    token: "sk-ant-oat-..."
```

At least one entry is required. The entry with `default: true` is used on
startup; if none is marked, the first entry is used.

### 2. Virtual API keys — `virtual_keys.yaml`

Copy the example and define keys for your clients:

```bash
cp virtual_keys.yaml.example virtual_keys.yaml
```

```yaml
virtual_keys:
  - name: alice
    key: "vk-alice-secret-key"
  - name: ci
    key: "vk-ci-secret-key"
```

Clients send these in `x-api-key` (or `Authorization: Bearer`). Usage is
tracked per name.

### 3. Environment — `.env`

```bash
cp .env.example .env
# edit .env and set your Tailscale node IP
```

### 4. Run

```bash
docker compose up -d
```

The proxy is available at `http://localhost:8181/v1/...`.

## Usage

Point any Anthropic SDK or tool at the proxy:

```bash
ANTHROPIC_BASE_URL=http://localhost:8181 \
ANTHROPIC_API_KEY=vk-alice-secret-key \
  claude ...
```

Or in code:

```python
import anthropic
client = anthropic.Anthropic(
    base_url="http://localhost:8181",
    api_key="vk-alice-secret-key",
)
```

## Admin UI

Open `http://<tailscale-ip>:8182` in a browser to:

- See 5h / 7d rate-limit utilization bars for each OAuth token
- Switch the active upstream token live
- View per-virtual-key request counts and token usage by model
- Access raw Anthropic rate-limit response headers

Auto-refreshes every 5 seconds.

## Prometheus Metrics

Available at `http://<tailscale-ip>:8182/metrics`:

| Metric | Labels | Description |
|--------|--------|-------------|
| `proxy_requests_total` | `key_name`, `model`, `status` | Total requests |
| `proxy_input_tokens_total` | `key_name`, `model` | Input tokens consumed |
| `proxy_output_tokens_total` | `key_name`, `model` | Output tokens consumed |
| `proxy_upstream_utilization_5h_ratio` | `token_name` | 5-hour utilization (0–1) |
| `proxy_upstream_utilization_7d_ratio` | `token_name` | 7-day utilization (0–1) |

## Files

| File | Purpose | Gitignored |
|------|---------|------------|
| `tokens.yaml` | Upstream OAuth tokens | Yes |
| `tokens.yaml.example` | Template for tokens.yaml | No |
| `virtual_keys.yaml` | Client virtual keys | No |
| `virtual_keys.yaml.example` | Template for virtual_keys.yaml | No |
| `.env` | `TAILSCALE_IP` | Yes |
| `.env.example` | Template for .env | No |
| `usage_stats.json` | Persisted usage data | No |
| `proxy.py` | Proxy + admin server | — |
| `docker-compose.yml` | Container config | — |
| `Dockerfile` | Image build | — |
