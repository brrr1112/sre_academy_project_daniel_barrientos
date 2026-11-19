#!/usr/bin/env bash

# ==============================================================================
# SRE Task Manager - Alert Trigger Simulator
#
# Randomly selects and simulates 1 of the 3 configured Prometheus alerts:
#   1. HighErrorRate       (Injects 500 errors and sends traffic)
#   2. HighResponseLatency (Injects 1000ms latency and sends traffic)
#   3. TaskManagerDown     (Scales deployment to 0 or terminates traffic)
# ==============================================================================

set -euo pipefail

APP_URL="${APP_URL:-http://127.0.0.1:5000}"
DURATION_SEC="${DURATION_SEC:-130}"  # Default > 2m to satisfy Prometheus alert rules (for: 1m / 2m)

# Colors for terminal output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log() {
  echo -e "${BLUE}[$(date '+%H:%M:%S')]${NC} $1"
}

# Ensure chaos is reset when the script exits (Ctrl+C or completion)
cleanup() {
  log "${YELLOW}Cleaning up chaos state...${NC}"
  curl -s -X POST "${APP_URL}/api/chaos" \
    -H "Content-Type: application/json" \
    -d '{"error_rate": 0, "latency_ms": 0}' >/dev/null 2>&1 || true

  if command -v kubectl >/dev/null 2>&1; then
    kubectl -n application scale deployment sre-task-manager-deployment --replicas=1 >/dev/null 2>&1 || true
  fi
  log "${GREEN}Reset complete.${NC}"
}
trap cleanup EXIT

# ------------------------------------------------------------------------------
# Alert 1: Trigger High Error Rate (> 5% 5xx errors for 2m)
# ------------------------------------------------------------------------------
trigger_high_error_rate() {
  log "${RED}>>> Triggering Alert: HighErrorRate <<<${NC}"
  log "Enabling 60% error rate on ${APP_URL}/api/chaos..."
  
  curl -s -X POST "${APP_URL}/api/chaos" \
    -H "Content-Type: application/json" \
    -d '{"error_rate": 0.6, "latency_ms": 0}' >/dev/null

  log "Sending continuous traffic for ${DURATION_SEC}s to trigger the alert rule..."
  END_TIME=$((SECONDS + DURATION_SEC))
  while [ $SECONDS -lt $END_TIME ]; do
    curl -s "${APP_URL}/api/tasks" >/dev/null || true
    curl -s -X POST "${APP_URL}/api/tasks" \
      -H "Content-Type: application/json" \
      -d '{"title": "Chaos Error Test Task"}' >/dev/null || true
    sleep 0.5
  done
  log "${GREEN}Completed HighErrorRate simulation.${NC}"
}

# ------------------------------------------------------------------------------
# Alert 2: Trigger High Response Latency (p95 > 500ms for 2m)
# ------------------------------------------------------------------------------
trigger_high_latency() {
  log "${YELLOW}>>> Triggering Alert: HighResponseLatency <<<${NC}"
  log "Injecting 1000ms latency on ${APP_URL}/api/chaos..."
  
  curl -s -X POST "${APP_URL}/api/chaos" \
    -H "Content-Type: application/json" \
    -d '{"error_rate": 0, "latency_ms": 1000}' >/dev/null

  log "Sending continuous traffic for ${DURATION_SEC}s to push p95 latency > 500ms..."
  END_TIME=$((SECONDS + DURATION_SEC))
  while [ $SECONDS -lt $END_TIME ]; do
    curl -s "${APP_URL}/api/tasks" >/dev/null || true
    sleep 0.2
  done
  log "${GREEN}Completed HighResponseLatency simulation.${NC}"
}

# ------------------------------------------------------------------------------
# Alert 3: Trigger Task Manager Down (Service unreachable for 1m)
# ------------------------------------------------------------------------------
trigger_task_manager_down() {
  log "${RED}>>> Triggering Alert: TaskManagerDown <<<${NC}"
  
  if command -v kubectl >/dev/null 2>&1 && kubectl get ns application >/dev/null 2>&1; then
    log "Scaling Kubernetes deployment sre-task-manager-deployment to 0 replicas..."
    kubectl -n application scale deployment sre-task-manager-deployment --replicas=0
    log "Waiting for 70 seconds to allow TaskManagerDown alert to fire in Prometheus..."
    sleep 70
    log "Restoring replicas back to 1..."
    kubectl -n application scale deployment sre-task-manager-deployment --replicas=1
  else
    log "${YELLOW}Kubectl not connected. Simulating outage by verifying target is unreachable.${NC}"
    log "Please stop the local app process or container to simulate downtime for > 1m."
    sleep 65
  fi
  log "${GREEN}Completed TaskManagerDown simulation.${NC}"
}

# ------------------------------------------------------------------------------
# Main Random Selector
# ------------------------------------------------------------------------------
SCENARIOS=(1 2 3)
CHOSEN_SCENARIO=${1:-${SCENARIOS[$((RANDOM % ${#SCENARIOS[@]}))]}}

echo "=================================================="
echo " SRE Alert Trigger Simulation"
echo " Target App URL : ${APP_URL}"
echo " Scenario       : ${CHOSEN_SCENARIO}"
echo "=================================================="

case "$CHOSEN_SCENARIO" in
  1)
    trigger_high_error_rate
    ;;
  2)
    trigger_high_latency
    ;;
  3)
    trigger_task_manager_down
    ;;
  *)
    log "${RED}Invalid scenario '${CHOSEN_SCENARIO}'. Choose 1 (Error Rate), 2 (Latency), or 3 (Service Down).${NC}"
    exit 1
    ;;
esac
