#!/bin/bash

# Deploys the SRE Task Manager and its monitoring stack on Minikube.
# Same structure as the exercises' cluster.sh (toolchain check -> start -> build -> apply -> wait).
#
# Optional environment variables (never commit real values):
#   SLACK_WEBHOOK_URL  Slack incoming webhook used by Alertmanager
#   SMTP_PASSWORD      SMTP / app password used by Alertmanager for email
#   RESET=true         Delete the project namespaces before deploying (clean slate)

set -e

# ----------------------
# Utility Functions
# ----------------------

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') - $1"
}

safe_run() {
  "$@" || { log "Command failed: $*"; exit 1; }
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

ensure_tool() {
  local tool="$1"
  if command_exists "$tool"; then
    log "$tool is installed."
  else
    log "$tool is not installed."
    if command_exists brew; then
      log "Installing $tool via Homebrew..."
      brew install "$tool" || { log "Failed to install $tool"; exit 1; }
    else
      log "Homebrew is missing. Please install it from https://brew.sh/"
      exit 1
    fi
  fi
}

start_if_not_running() {
  local svc="$1"
  if command_exists "$svc"; then
    "$svc" status >/dev/null 2>&1 || {
      log "Starting $svc..."
      safe_run "$svc" start
      sleep 5
    }
  else
    log "Cannot start $svc — binary not found."
    exit 1
  fi
}

pause() {
  log "Waiting for $1 seconds..."
  sleep "$1"
}

# Random alphanumeric string (used for the Grafana admin password)
random_string() {
  LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1"
}

APP_IMAGE="sre-task-manager:1.0.0"

# -------------------------------
# Toolchain Check (before anything else)
# -------------------------------
log "Checking toolchain dependencies..."
for tool in colima minikube kubectl docker; do
  ensure_tool "$tool"
done
pause 2

# -------------------------------
# Startup Phase (Colima + Minikube)
# -------------------------------
log "Initializing and Starting Colima (for Minikube Docker driver)..."
start_if_not_running colima
pause 5

log "Starting Minikube with Docker driver..."
minikube status >/dev/null 2>&1 || safe_run minikube start --driver=docker
pause 5

# -------------------------------
# Kubernetes Cluster Cleanup (only when RESET=true)
# -------------------------------
if [ "${RESET:-false}" = "true" ]; then
  log "Cleaning up project namespaces..."
  safe_run kubectl delete ns application opentelemetry monitoring --ignore-not-found=true
  safe_run kubectl delete clusterrole,clusterrolebinding prometheus --ignore-not-found=true
  pause 5
fi

# -------------------------------
# Docker Image Build
# (The Dockerfile base image lives in registry.redhat.io: run `docker login registry.redhat.io` first)
# -------------------------------
log "Building Docker image..."
safe_run docker build -t "$APP_IMAGE" ./app
pause 2

log "Loading the image into Minikube..."
safe_run minikube image load "$APP_IMAGE"
pause 2

# -------------------------------
# Kubernetes Deployment
# -------------------------------
log "Creating namespaces..."
safe_run kubectl apply -f ./k8s/namespaces.yaml

# Secrets are created from environment variables, never stored in Git.
log "Creating secrets..."
if [ -z "${SLACK_WEBHOOK_URL:-}" ]; then
  log "SLACK_WEBHOOK_URL is not set: using a placeholder (Slack notifications will fail until you set it)."
  SLACK_WEBHOOK_URL="https://hooks.slack.com/services/REPLACE/ME/PLEASE"
fi
if [ -z "${SMTP_PASSWORD:-}" ]; then
  log "SMTP_PASSWORD is not set: using a placeholder (email notifications will fail until you set it)."
  SMTP_PASSWORD="replace-me"
fi
safe_run kubectl -n monitoring create secret generic alertmanager-secrets \
  --from-literal=slack-webhook-url="$SLACK_WEBHOOK_URL" \
  --from-literal=smtp-password="$SMTP_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f -

# Keep the existing Grafana password if the secret already exists
if ! kubectl -n monitoring get secret grafana-admin >/dev/null 2>&1; then
  safe_run kubectl -n monitoring create secret generic grafana-admin \
    --from-literal=admin-password="$(random_string 20)"
fi

log "Applying Kubernetes configurations..."
# The manifest points to a registry image; swap it for the locally built one.
sed "s|image: ghcr.io/YOUR-USERNAME/sre-task-manager:1.0.0|image: $APP_IMAGE|" ./k8s/application.yaml \
  | safe_run kubectl apply -f -
for file in \
  ./k8s/jaeger.yaml \
  ./k8s/prometheus.yaml \
  ./k8s/prometheus-rbac-cluster.yaml \
  ./k8s/alertmanager.yaml \
  ./k8s/grafana.yaml
do
  safe_run kubectl apply -f "$file"
done
pause 5

# -------------------------------
# Pod Initialization Wait
# -------------------------------
log "Waiting for pod readiness..."
kubectl wait --for=condition=Ready pods --all -n application --timeout=180s || log "Some application pods failed to become Ready within timeout"
kubectl wait --for=condition=Ready pods --all -n monitoring --timeout=180s || log "Some monitoring pods failed to become Ready within timeout"
kubectl wait --for=condition=Ready pods --all -n opentelemetry --timeout=180s || log "Jaeger failed to become Ready within timeout"

safe_run kubectl get pods -A
log "Script finished. Grafana admin password:"
log "  kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 --decode"

log "Opening the UIs in your browser..."
minikube service sre-task-manager-service -n application > /dev/null 2>&1 &
minikube service grafana-service -n monitoring > /dev/null 2>&1 &
minikube service prometheus-service -n monitoring > /dev/null 2>&1 &
minikube service alertmanager -n monitoring > /dev/null 2>&1 &
minikube service jaeger-service -n opentelemetry > /dev/null 2>&1 &
