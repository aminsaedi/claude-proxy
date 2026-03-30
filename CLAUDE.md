# CLAUDE.md

## Project overview

`claude-proxy` is a single-file Python proxy (`proxy.py`) that forwards
Anthropic API requests from clients using virtual API keys to the real
`api.anthropic.com` using OAuth tokens. It runs two FastAPI apps concurrently:
a proxy on port 8080 and an admin UI on port 8090.

## Key files

- `proxy.py` — entire application logic (proxy + admin)
- `tokens.yaml` — upstream OAuth tokens (**gitignored**, never commit)
- `virtual_keys.yaml` — client virtual key definitions
- `usage_stats.json` — persisted per-key usage counters (updated live)
- `docker-compose.yml` — maps host ports 8181 (proxy) and 8182 (admin)
- `.env` — contains `TAILSCALE_IP` (**gitignored**, never commit)

## Running locally (without Docker)

```bash
pip install -r requirements.txt
python proxy.py
```

Requires `tokens.yaml` and `virtual_keys.yaml` in the same directory.

## Common tasks

### Add a new virtual key
Edit `virtual_keys.yaml` and restart the container (`docker compose restart`).
The proxy loads keys once at startup.

### Add / rotate an OAuth token
Edit `tokens.yaml` and restart. The active token can be switched live via
the admin UI without restart.

### Check logs
```bash
docker compose logs -f
```

### Rebuild after code changes
```bash
docker compose up -d --build
```

## Architecture notes

- **Auth flow**: client sends virtual key → proxy validates against
  `VIRTUAL_KEYS`, swaps in the active OAuth token as `Authorization: Bearer`,
  injects `anthropic-beta: oauth-2025-04-20`, forwards to upstream.
- **Streaming**: uses `httpx` streaming + async generator; token usage is
  parsed from SSE events (`message_start`, `message_delta`).
- **Usage tracking**: stored in-memory and flushed to `usage_stats.json` after
  every request. The JSON file is bind-mounted so data survives container
  restarts.
- **Rate-limit headers**: `anthropic-ratelimit-*` headers from upstream are
  captured per OAuth token and exposed in the admin UI and Prometheus gauges.
- **Admin port binding**: admin is bound to `${TAILSCALE_IP}:8182` in
  `docker-compose.yml` — never expose it on a public interface.

## What NOT to do

- Do not commit `tokens.yaml` or `.env` — they contain secrets.
- Do not expose port 8182 (admin) publicly; it has no authentication.
- Do not add authentication to the proxy path itself — it relies on
  virtual keys kept out of source control.
