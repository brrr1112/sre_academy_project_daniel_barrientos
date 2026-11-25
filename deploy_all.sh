#!/bin/bash
# Runs every step of deployment_steps.html for the SRE Task Manager on macOS:
#   Colima -> Minikube -> deploy.sh (image + manifests) -> SSH NodePort tunnel -> verification.
# Idempotent: safe to re-run.
#
# Usage:
#   ./deploy_all.sh [image.tar]
#     image.tar can be an absolute path, a path relative to the current directory,
#     or just a file name located next to this script.
#     default image: sre-task-manager-main-d44721d-arm64-250127.tar (next to this script)
#   Example:
#     ./deploy_all.sh /Users/danielbarrientos/Desktop/SRE/SRE_APP/sre-task-manager-main-d44721d-arm64-250127.tar
#
# Optional environment variables (never commit real values):
#   SLACK_WEBHOOK_URL  Slack incoming webhook used by Alertmanager
#   SMTP_PASSWORD      SMTP / app password used by Alertmanager for email
#   RESET=true         Delete the project namespaces first (clean slate)
#   COLIMA_CPU         CPUs when Colima has to be started (default 4)
#   COLIMA_MEMORY      GiB of RAM when Colima has to be started (default 6)

set -euo pipefail

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') - $1"; }
die() { log "ERROR: $1"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# deploy.sh lives next to this script (project folder) or in a project subfolder
if [ -f "$SCRIPT_DIR/deploy.sh" ]; then
  PROJECT_DIR="$SCRIPT_DIR"
else
  PROJECT_DIR="$SCRIPT_DIR/sre_academy_project_daniel_barrientos"
fi
DEFAULT_TAR="sre-task-manager-main-d44721d-arm64-250127.tar"
# Default image: next to this script, otherwise in the parent folder
if [ -f "$SCRIPT_DIR/$DEFAULT_TAR" ]; then
  DEFAULT_TAR="$SCRIPT_DIR/$DEFAULT_TAR"
else
  DEFAULT_TAR="$SCRIPT_DIR/../$DEFAULT_TAR"
fi
TAR_ARG="${1:-$DEFAULT_TAR}"
PORTS=(30007 30093 30030 30090)

# Resolve to an absolute path (deploy.sh changes directory, so relative paths would break)
if [ -f "$TAR_ARG" ]; then
  TAR_FILE="$(cd "$(dirname "$TAR_ARG")" && pwd)/$(basename "$TAR_ARG")"
elif [ -f "$SCRIPT_DIR/$TAR_ARG" ]; then
  TAR_FILE="$SCRIPT_DIR/$TAR_ARG"
else
  die "Image archive not found: $TAR_ARG"
fi
[ -x "$PROJECT_DIR/deploy.sh" ] || die "deploy.sh not found or not executable in $PROJECT_DIR"
for tool in colima docker minikube kubectl ssh curl; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed"
done

# 1. Container runtime
if colima status >/dev/null 2>&1; then
  log "Colima already running."
else
  log "Starting Colima..."
  colima start --cpu "${COLIMA_CPU:-4}" --memory "${COLIMA_MEMORY:-6}"
fi
log "Docker server version: $(docker version --format '{{.Server.Version}}')"

# 2. Minikube
if minikube status >/dev/null 2>&1; then
  log "Minikube already running."
else
  log "Starting Minikube..."
  minikube start --driver=docker
fi
kubectl get nodes

# 3. Load image and deploy everything (namespaces, secrets, prometheus, alertmanager, grafana, app)
log "Running deploy.sh with $TAR_FILE"
"$PROJECT_DIR/deploy.sh" "$TAR_FILE"

# 4. SSH tunnel so the NodePorts are reachable on 127.0.0.1 (Docker driver on macOS)
log "Setting up SSH tunnel for NodePorts: ${PORTS[*]}"
for p in "${PORTS[@]}"; do
  pkill -f "ssh.*${p}:127.0.0.1:${p}" 2>/dev/null || true
done
sleep 1

SSH_KEY="$(minikube ssh-key)"
SSH_PORT="$(docker port minikube 22/tcp | head -n 1 | sed 's/.*://')"
[ -n "$SSH_PORT" ] || die "Could not determine Minikube SSH port"

FORWARDS=()
for p in "${PORTS[@]}"; do
  FORWARDS+=(-L "127.0.0.1:${p}:127.0.0.1:${p}")
done
ssh -i "$SSH_KEY" \
  -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null \
  -o LogLevel=ERROR \
  -o ExitOnForwardFailure=yes \
  -p "$SSH_PORT" \
  -N -f \
  "${FORWARDS[@]}" \
  docker@127.0.0.1

# 5. Verification
check() { # name, url, expected substring (empty = only HTTP success)
  local name="$1" url="$2" expect="$3" body="" i
  for i in $(seq 1 15); do
    if body="$(curl -fsS --max-time 5 "$url" 2>/dev/null)"; then
      if [ -z "$expect" ] || echo "$body" | grep -q "$expect"; then
        log "OK    $name ($url)"
        return 0
      fi
    fi
    sleep 2
  done
  log "FAIL  $name ($url)"
  return 1
}

log "Verifying endpoints..."
FAILED=0
check "App /healthz"   "http://127.0.0.1:30007/healthz" ""                  || FAILED=1
check "App /readyz"    "http://127.0.0.1:30007/readyz" ""                   || FAILED=1
check "Alertmanager"   "http://127.0.0.1:30093/-/ready" "OK"                || FAILED=1
check "Grafana login"  "http://127.0.0.1:30030/login" ""                    || FAILED=1
check "Prometheus"     "http://127.0.0.1:30090/-/ready" "Ready"             || FAILED=1

echo
log "Local URLs:"
echo "  Application   http://127.0.0.1:30007/"
echo "  Alertmanager  http://127.0.0.1:30093/#/alerts"
echo "  Grafana       http://127.0.0.1:30030/login   (user: admin)"
echo "  Prometheus    http://127.0.0.1:30090/query"
echo "Grafana password:"
echo "  kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 --decode; echo"
echo "Stop tunnel:  pkill -f 'ssh.*30007:127.0.0.1:30007'"

[ "$FAILED" -eq 0 ] || die "One or more verification checks failed"
log "Deployment complete."
