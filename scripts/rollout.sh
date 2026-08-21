#!/usr/bin/env bash
# Roll claude-proxy to a new image and *prove* the rollout was zero-downtime.
#
# A rollout that "looked fine" is not evidence. This drives continuous traffic
# through the public endpoint for the whole rollout and counts every response,
# so a single dropped or refused request shows up as a failure rather than as a
# blip nobody happened to be watching for.
#
#   ./scripts/rollout.sh v9                       # roll, watch, verify
#   ./scripts/rollout.sh v9 --dry-run             # show what would happen
#   ./scripts/rollout.sh --verify-only            # just probe, don't deploy
#
# Requires: kubectl with access to the cluster, and a virtual key in $VK.
set -euo pipefail

NS=claude-proxy
DEPLOY=claude-proxy
CONTAINER=proxy
REGISTRY=100.69.180.101:31500
ENDPOINT="${ENDPOINT:-https://claude-proxy.aminsaedi.com}"
PROBE_INTERVAL="${PROBE_INTERVAL:-0.25}"
MANIFEST="$(cd "$(dirname "$0")/.." && pwd)/k8s/claude-proxy.yaml"

TAG="${1:-}"
[[ "${TAG}" == --* ]] && TAG=""
DRY_RUN=false
VERIFY_ONLY=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --verify-only) VERIFY_ONLY=true ;;
  esac
done

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

# --- the probe -------------------------------------------------------------
# /healthz on the public endpoint: cheap, needs no key, and — crucially — is the
# same signal the ingress uses to decide where to route, so a gap here is a gap
# a real client would have seen.
PROBE_LOG=$(mktemp)
probe_loop() {
  while :; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${ENDPOINT}/healthz" || echo 000)
    printf '%s %s\n' "$(date +%s.%N)" "$code" >> "$PROBE_LOG"
    sleep "$PROBE_INTERVAL"
  done
}

report() {
  local total ok bad
  total=$(wc -l < "$PROBE_LOG")
  ok=$(awk '$2 == 200' "$PROBE_LOG" | wc -l)
  bad=$((total - ok))
  say "Result"
  printf '  probes sent : %s\n  200 OK      : %s\n  not OK      : %s\n' "$total" "$ok" "$bad"
  if [[ "$bad" -gt 0 ]]; then
    printf '\n  \033[31mnon-200 responses:\033[0m\n'
    awk '$2 != 200 {print "    " strftime("%H:%M:%S", int($1)) "  HTTP " $2}' "$PROBE_LOG"
    printf '\n  \033[31mFAIL — the rollout dropped traffic.\033[0m\n'
    rm -f "$PROBE_LOG"
    return 1
  fi
  printf '\n  \033[32mPASS — every request was served throughout the rollout.\033[0m\n'
  rm -f "$PROBE_LOG"
}

if $DRY_RUN; then
  say "Dry run"
  kubectl -n "$NS" diff -f "$MANIFEST" || true
  exit 0
fi

if ! $VERIFY_ONLY; then
  [[ -n "$TAG" ]] || { echo "usage: $0 <image-tag> [--dry-run]" >&2; exit 2; }
  say "Pre-flight"
  kubectl -n "$NS" get deploy "$DEPLOY" -o wide
  echo "  target image: ${REGISTRY}/${DEPLOY}:${TAG}"
fi

say "Starting probe against ${ENDPOINT}"
probe_loop & PROBE_PID=$!
trap 'kill "$PROBE_PID" 2>/dev/null || true' EXIT
sleep 3   # a short baseline before anything changes

if ! $VERIFY_ONLY; then
  say "Applying manifest"
  kubectl apply -f "$MANIFEST"

  # apply already carries the new tag, but set it explicitly so re-running with
  # the same manifest and a new tag still triggers a rollout.
  kubectl -n "$NS" set image "deploy/$DEPLOY" \
    "${CONTAINER}=${REGISTRY}/${DEPLOY}:${TAG}" --record=false

  say "Waiting for the rollout"
  kubectl -n "$NS" rollout status "deploy/$DEPLOY" --timeout=10m

  say "Settling"
  sleep 15   # keep probing after the old pod goes, to catch a late gap
fi

kill "$PROBE_PID" 2>/dev/null || true
wait "$PROBE_PID" 2>/dev/null || true
trap - EXIT

if ! $VERIFY_ONLY; then
  say "Now running"
  kubectl -n "$NS" get pods -o wide
  kubectl -n "$NS" get deploy "$DEPLOY" \
    -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
fi

report
