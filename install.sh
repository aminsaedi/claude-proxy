#!/usr/bin/env bash
# install.sh — set up and start claude-proxy
set -euo pipefail

BOLD='\033[1m'; CYAN='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; DIM='\033[2m'; RESET='\033[0m'
info()    { echo -e "${CYAN}${BOLD}▶${RESET} $*"; }
success() { echo -e "${GREEN}${BOLD}✓${RESET} $*"; }
warn()    { echo -e "${YELLOW}${BOLD}!${RESET} $*"; }
error()   { echo -e "${RED}${BOLD}✗${RESET} $*" >&2; }
ask()     { echo -en "${CYAN}${BOLD}$1${RESET} "; }

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_UID=10001

echo; echo -e "${BOLD}Claude Proxy — Installer${RESET}"; echo -e "${DIM}Sets up data/ and starts the container.${RESET}"; echo

command -v docker &>/dev/null || { error "Docker is not installed."; exit 1; }
docker compose version &>/dev/null || { error "Docker Compose v2 is required."; exit 1; }
success "Docker $(docker --version | awk '{print $3}' | tr -d ',')"

mkdir -p data

# --- tokens ---------------------------------------------------------------
if [[ -f data/tokens.yaml ]]; then
    warn "data/tokens.yaml exists — skipping."
else
    info "Configure an upstream Anthropic OAuth token"
    ask "Token name [personal]:"; read -r TOKEN_NAME; TOKEN_NAME="${TOKEN_NAME:-personal}"
    ask "OAuth token (sk-ant-oat-...):"; read -rs TOKEN_VALUE; echo
    [[ -n "$TOKEN_VALUE" ]] || { error "Token value is required."; exit 1; }
    cat > data/tokens.yaml <<EOF
tokens:
  - name: ${TOKEN_NAME}
    token: "${TOKEN_VALUE}"
    default: true
EOF
    success "data/tokens.yaml created."
fi

# --- virtual key ----------------------------------------------------------
if [[ -f data/virtual_keys.yaml ]]; then
    warn "data/virtual_keys.yaml exists — skipping."
else
    info "Create a virtual API key for clients"
    ask "Key name [default]:"; read -r KEY_NAME; KEY_NAME="${KEY_NAME:-default}"
    ask "Key value (blank to auto-generate):"; read -r KEY_VALUE
    [[ -n "$KEY_VALUE" ]] || KEY_VALUE="vk-$(openssl rand -base64 18 | tr -d '/+=' | head -c 24)"
    cat > data/virtual_keys.yaml <<EOF
virtual_keys:
  - name: ${KEY_NAME}
    key: "${KEY_VALUE}"
EOF
    success "data/virtual_keys.yaml created."; echo -e "${DIM}  Key: ${KEY_VALUE}${RESET}"
fi

[[ -f data/config.yaml ]] || { cp config.yaml.example data/config.yaml; success "data/config.yaml created."; }
[[ -f data/usage_stats.json ]] || echo "{}" > data/usage_stats.json

# --- .env (admin UI / Tailscale) -----------------------------------------
if [[ -f .env ]]; then
    warn ".env exists — skipping."
else
    info "Admin UI (Tailscale-only, HTTP Basic auth)"
    ask "Tailscale IP [127.0.0.1]:"; read -r TAILSCALE_IP; TAILSCALE_IP="${TAILSCALE_IP:-127.0.0.1}"
    ask "Admin password (blank to auto-generate):"; read -rs ADMIN_PASSWORD; echo
    [[ -n "$ADMIN_PASSWORD" ]] || ADMIN_PASSWORD="$(openssl rand -base64 18 | tr -d '/+=' | head -c 24)"
    cat > .env <<EOF
TAILSCALE_IP=${TAILSCALE_IP}
ADMIN_USER=admin
ADMIN_PASSWORD=${ADMIN_PASSWORD}
EOF
    success ".env created — admin UI at http://${TAILSCALE_IP}:8182 (user: admin)"
    echo -e "${DIM}  Password: ${ADMIN_PASSWORD}${RESET}"
fi

# --- ownership + start ----------------------------------------------------
info "Setting data/ ownership to container uid ${APP_UID}…"
chown -R "${APP_UID}:${APP_UID}" data 2>/dev/null || sudo chown -R "${APP_UID}:${APP_UID}" data

info "Building and starting…"; echo
docker compose up -d --build

echo; success "claude-proxy is running."
echo -e "  ${BOLD}Proxy:${RESET}    http://localhost:8181"
TIP="$(grep -E '^TAILSCALE_IP=' .env | cut -d= -f2 || true)"
[[ -n "$TIP" ]] && echo -e "  ${BOLD}Admin UI:${RESET} http://${TIP}:8182"
echo -e "  ${BOLD}Manage:${RESET}   docker compose exec -it proxy python manage.py"
echo -e "  ${BOLD}Logs:${RESET}     docker compose logs -f"; echo
