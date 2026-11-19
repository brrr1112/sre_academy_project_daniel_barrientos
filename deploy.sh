#!/bin/bash
# Deploys the SRE Task Manager + Prometheus + Alertmanager + Grafana on Minikube.
# Idempotent: safe to re-run.
#
# Usage:
#   ./deploy.sh                    build the image from ./App and deploy
#   ./deploy.sh <image.tar>        load an image archive (e.g. from CI) and deploy
#
# Optional environment variables (never commit real values):
#   SLACK_WEBHOOK_URL  Slack incoming webhook used by Alertmanager
#   SMTP_PASSWORD      SMTP / app password used by Alertmanager for email
#   RESET=true         Delete the project namespaces first (clean slate)

set -euo pipefail

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') - $1"; }
die() { log "ERROR: $1"; exit 1; }

cd "$(dirname "$0")"

TAR_FILE="${1:-}"
[ -z "$TAR_FILE" ] || [ -f "$TAR_FILE" ] || die "File not found: $TAR_FILE"

for tool in docker minikube kubectl; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed"
done

# 1. Make sure the cluster is running
minikube status >/dev/null 2>&1 || { log "Starting Minikube..."; minikube start --driver=docker; }
kubectl config use-context minikube >/dev/null

# 2. Get the image into Minikube: build it, or load the CI archive
if [ -n "$TAR_FILE" ]; then
  log "Loading $TAR_FILE into Docker..."
  LOAD_OUTPUT="$(docker load -i "$TAR_FILE")"
  echo "$LOAD_OUTPUT"
  APP_IMAGE="$(echo "$LOAD_OUTPUT" | sed -n 's/^Loaded image: //p' | tail -n 1)"
  [ -n "$APP_IMAGE" ] || die "Could not read the image name from 'docker load' output"
else
  APP_IMAGE="sre-task-manager:1.0.0"
  log "Building $APP_IMAGE..."
  docker build -t "$APP_IMAGE" ./App
fi
log "Loading $APP_IMAGE into Minikube..."
minikube image load "$APP_IMAGE"

# 3. Optional clean slate
if [ "${RESET:-false}" = "true" ]; then
  log "Deleting project namespaces..."
  kubectl delete ns application monitoring --ignore-not-found=true
  kubectl delete clusterrole,clusterrolebinding prometheus --ignore-not-found=true
fi

# 4. Namespaces first (secrets need them)
kubectl apply -f ./kube/namespaces.yaml

# 5. Secrets come from the environment, never from Git
SLACK_WEBHOOK_URL="${SLACK_WEBHOOK_URL:-https://hooks.slack.com/services/REPLACE/ME/PLEASE}"
SMTP_PASSWORD="${SMTP_PASSWORD:-replace-me}"
kubectl -n monitoring create secret generic alertmanager-secrets \
  --from-literal=slack-webhook-url="$SLACK_WEBHOOK_URL" \
  --from-literal=smtp-password="$SMTP_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f -

# Keep the Grafana password if the secret already exists
if ! kubectl -n monitoring get secret grafana-admin >/dev/null 2>&1; then
  kubectl -n monitoring create secret generic grafana-admin \
    --from-literal=admin-password="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20)"
fi

# 6. Apply one directory per component
log "Applying manifests..."
kubectl apply -f ./kube/prometheus/
kubectl apply -f ./kube/alertmanager/
kubectl apply -f ./kube/grafana/
# The app manifest has a placeholder image: swap it for the one that was just loaded
for file in configmap deployment service; do
  sed "s|image: ghcr.io/YOUR-USERNAME/sre-task-manager:1.0.0|image: $APP_IMAGE|" "./kube/app/${file}.yaml" \
    | kubectl apply -f -
done
# The same tag can be rebuilt: force the pod to pick up the new image
kubectl -n application rollout restart deployment/sre-task-manager >/dev/null

# 7. Wait until everything is healthy
log "Waiting for rollouts..."
kubectl -n application rollout status deployment/sre-task-manager --timeout=180s
for d in prometheus-deployment alertmanager-deployment grafana-deployment; do
  kubectl -n monitoring rollout status "deployment/$d" --timeout=180s
done

# 8. Report
MINIKUBE_IP="$(minikube ip)"
kubectl get pods,svc,pvc -A | grep -E "NAMESPACE|application|monitoring"
echo
log "Endpoints (reachable from this machine; with the Docker driver on macOS use 'minikube service <name> -n <ns>' instead):"
echo "  Application   http://${MINIKUBE_IP}:30007"
echo "  Prometheus    http://${MINIKUBE_IP}:30090"
echo "  Alertmanager  http://${MINIKUBE_IP}:30093"
echo "  Grafana       http://${MINIKUBE_IP}:30030   (user: admin)"
echo "Grafana password:"
echo "  kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 --decode; echo"
