#!/usr/bin/env bash
# install.sh — set up and start claude-proxy
set -euo pipefail

BOLD='\033[1m'
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
DIM='\033[2m'
RESET='\033[0m'

info()    { echo -e "${CYAN}${BOLD}▶${RESET} $*"; }
success() { echo -e "${GREEN}${BOLD}✓${RESET} $*"; }
warn()    { echo -e "${YELLOW}${BOLD}!${RESET} $*"; }
error()   { echo -e "${RED}${BOLD}✗${RESET} $*" >&2; }
ask()     { echo -en "${CYAN}${BOLD}$1${RESET} "; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo
echo -e "${BOLD}Claude Proxy — Installer${RESET}"
echo -e "${DIM}Sets up config files and starts the Docker container.${RESET}"
echo

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

if ! command -v docker &>/dev/null; then
    error "Docker is not installed. Install it from https://docs.docker.com/get-docker/"
    exit 1
fi

if ! docker compose version &>/dev/null 2>&1; then
    error "Docker Compose v2 is not available. Update Docker Desktop or install the plugin."
    exit 1
fi

success "Docker $(docker --version | awk '{print $3}' | tr -d ',')"
success "Docker Compose $(docker compose version --short)"
echo

# ---------------------------------------------------------------------------
# tokens.yaml
# ---------------------------------------------------------------------------

if [[ -f tokens.yaml ]]; then
    warn "tokens.yaml already exists — skipping token setup."
    warn "Edit it manually or use: docker compose exec -it proxy python manage.py"
    echo
else
    info "Configure upstream Anthropic OAuth token"
    echo -e "${DIM}You can add more tokens later via the manager (manage.py).${RESET}"
    echo

    ask "Token name (e.g. personal):"
    read -r TOKEN_NAME
    TOKEN_NAME="${TOKEN_NAME:-personal}"

    ask "OAuth token (sk-ant-oat-...):"
    read -rs TOKEN_VALUE
    echo
    if [[ -z "$TOKEN_VALUE" ]]; then
        error "Token value is required."
        exit 1
    fi

    cat > tokens.yaml <<EOF
tokens:
  - name: ${TOKEN_NAME}
    token: "${TOKEN_VALUE}"
    default: true
EOF
    success "tokens.yaml created."
    echo
fi

# ---------------------------------------------------------------------------
# virtual_keys.yaml
# ---------------------------------------------------------------------------

if [[ -f virtual_keys.yaml ]]; then
    warn "virtual_keys.yaml already exists — skipping key setup."
    echo
else
    info "Create a virtual API key for clients"
    echo -e "${DIM}Clients send this key as x-api-key. Leave blank to auto-generate.${RESET}"
    echo

    ask "Key name (e.g. alice):"
    read -r KEY_NAME
    KEY_NAME="${KEY_NAME:-default}"

    ask "Key value (leave blank to auto-generate):"
    read -r KEY_VALUE
    if [[ -z "$KEY_VALUE" ]]; then
        KEY_VALUE="vk-$(openssl rand -base64 18 | tr -d '/+=' | head -c 24)"
    fi

    cat > virtual_keys.yaml <<EOF
virtual_keys:
  - name: ${KEY_NAME}
    key: "${KEY_VALUE}"
EOF
    success "virtual_keys.yaml created."
    echo -e "${DIM}  Key: ${KEY_VALUE}${RESET}"
    echo
fi

# ---------------------------------------------------------------------------
# .env — admin UI / Tailscale
# ---------------------------------------------------------------------------

if [[ -f .env ]]; then
    warn ".env already exists — skipping admin UI setup."
    echo
else
    info "Admin UI (port 8182)"
    echo -e "${DIM}The admin UI lets you switch tokens, view usage, and access Prometheus metrics.${RESET}"
    echo -e "${DIM}It should only be exposed on a private network (Tailscale recommended).${RESET}"
    echo

    ask "Enable admin UI on Tailscale? [y/N]:"
    read -r WANT_TAILSCALE

    if [[ "${WANT_TAILSCALE,,}" == "y" ]]; then
        ask "Tailscale IP (e.g. 100.x.x.x):"
        read -r TAILSCALE_IP
        if [[ -z "$TAILSCALE_IP" ]]; then
            error "Tailscale IP is required."
            exit 1
        fi
        echo "TAILSCALE_IP=${TAILSCALE_IP}" > .env
        success ".env created — admin UI will be accessible at http://${TAILSCALE_IP}:8182"
    else
        echo "TAILSCALE_IP=127.0.0.1" > .env
        success ".env created — admin UI bound to localhost only (http://127.0.0.1:8182)"
    fi
    echo
fi

# ---------------------------------------------------------------------------
# usage_stats.json — must exist as a file for the bind mount
# ---------------------------------------------------------------------------

if [[ ! -f usage_stats.json ]]; then
    echo "{}" > usage_stats.json
fi

# ---------------------------------------------------------------------------
# Build and start
# ---------------------------------------------------------------------------

info "Building and starting the container…"
echo
docker compose up -d --build

echo
success "claude-proxy is running."
echo
echo -e "  ${BOLD}Proxy:${RESET}    http://localhost:8181"

TAILSCALE_IP_VAL="$(grep TAILSCALE_IP .env | cut -d= -f2 || true)"
if [[ -n "$TAILSCALE_IP_VAL" ]]; then
    echo -e "  ${BOLD}Admin UI:${RESET}  http://${TAILSCALE_IP_VAL}:8182"
fi

echo
echo -e "  ${BOLD}Manage keys/tokens:${RESET}"
echo -e "  ${DIM}docker compose exec -it proxy python manage.py${RESET}"
echo
echo -e "  ${BOLD}Logs:${RESET}"
echo -e "  ${DIM}docker compose logs -f${RESET}"
echo
