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
# kubectl runs locally by default. The API server here only listens on the
# control plane's loopback, so set KUBECTL_SSH to drive it from there instead:
#
#   KUBECTL_SSH=amin@mx ./scripts/rollout.sh v9
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

# kubectl, wherever it has to run from.
KUBECTL_SSH="${KUBECTL_SSH:-}"
kube() {
  if [[ -n "$KUBECTL_SSH" ]]; then
    ssh -o BatchMode=yes "$KUBECTL_SSH" kubectl "$@"
  else
    kubectl "$@"
  fi
}
# apply reads the manifest from *this* machine either way, so a remote kubectl
# is fed on stdin rather than being asked for a path it cannot see.
kube_apply() {
  if [[ -n "$KUBECTL_SSH" ]]; then
    ssh -o BatchMode=yes "$KUBECTL_SSH" "kubectl apply -f -" < "$MANIFEST"
  else
    kubectl apply -f "$MANIFEST"
  fi
}

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
  if [[ -n "$KUBECTL_SSH" ]]; then
    ssh -o BatchMode=yes "$KUBECTL_SSH" "kubectl -n $NS diff -f -" < "$MANIFEST" || true
  else
    kubectl -n "$NS" diff -f "$MANIFEST" || true
  fi
  exit 0
fi

if ! $VERIFY_ONLY; then
  [[ -n "$TAG" ]] || { echo "usage: $0 <image-tag> [--dry-run]" >&2; exit 2; }
  say "Pre-flight"
  kube -n "$NS" get deploy "$DEPLOY" -o wide
  echo "  target image: ${REGISTRY}/${DEPLOY}:${TAG}"
fi

say "Starting probe against ${ENDPOINT}"
probe_loop & PROBE_PID=$!
trap 'kill "$PROBE_PID" 2>/dev/null || true' EXIT
sleep 3   # a short baseline before anything changes

if ! $VERIFY_ONLY; then
  say "Applying manifest"
  kube_apply

  # apply already carries the new tag, but set it explicitly so re-running with
  # the same manifest and a new tag still triggers a rollout.
  kube -n "$NS" set image "deploy/$DEPLOY" \
    "${CONTAINER}=${REGISTRY}/${DEPLOY}:${TAG}"

  say "Waiting for the rollout"
  kube -n "$NS" rollout status "deploy/$DEPLOY" --timeout=10m

  say "Settling"
  sleep 15   # keep probing after the old pod goes, to catch a late gap
fi

kill "$PROBE_PID" 2>/dev/null || true
wait "$PROBE_PID" 2>/dev/null || true
trap - EXIT

if ! $VERIFY_ONLY; then
  say "Now running"
  kube -n "$NS" get pods -o wide
  # No jsonpath here: a remote kubectl runs through a second shell, which eats
  # the backslash in {"\n"} and makes kubectl reject the expression.
  kube -n "$NS" get deploy "$DEPLOY" \
    -o go-template='{{(index .spec.template.spec.containers 0).image}}'
  echo
fi

report
