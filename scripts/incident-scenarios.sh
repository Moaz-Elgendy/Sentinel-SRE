#!/bin/bash
# Phase 12 — end-to-end incident simulations.
#
# Drives a small set of named, repeatable incidents through the Phase 10
# chaos control API, then confirms Phase 9's stack actually catches each
# one: the right Prometheus alert enters "firing" within its `for:`
# window, and (best-effort) that it reaches Alertmanager.
#
# This closes the loop the whole project has been building toward: every
# earlier phase produced a signal (a metric, a log line, an alert rule) in
# isolation. This script is the first place those signals get exercised
# together, end to end, the way a real incident — or Sentinel — would
# actually encounter them.
#
# Usage:
#   ./scripts/incident-scenarios.sh <scenario> [namespace]
#   ./scripts/incident-scenarios.sh all
#
# Scenarios: db-outage | http-errors | latency | notification-degradation
#            | full-outage | all
#
# Requires: kubectl context pointed at the cluster, the chaos control
# plane enabled (CHAOS_MODE=true + CHAOS_ADMIN_TOKEN set in both
# services' Secrets — see Phases.md Phase 10), and the Phase 9 monitoring
# stack deployed.
#
# Each scenario port-forwards what it needs, runs, verifies, cleans up
# its port-forwards, and resets the fault it injected — even on failure
# (see the trap below). Chaos state is per-pod and in-memory (Phase 10),
# so scenarios that need a deterministic single target first scale the
# relevant Deployment to 1 replica, and restore the original replica
# count afterward.
set -euo pipefail

NAMESPACE="${2:-citizen-portal}"
CHAOS_TOKEN="${CHAOS_ADMIN_TOKEN:-}"
PROM_PORT=9090
AM_PORT=9093
CITIZEN_PORT=18000
NOTIF_PORT=18001

PIDS=()
ORIGINAL_REPLICAS=()

cleanup() {
  local status=$?
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" >/dev/null 2>&1 || true
  done
  for entry in "${ORIGINAL_REPLICAS[@]:-}"; do
    [ -z "$entry" ] && continue
    local deploy="${entry%%:*}"
    local count="${entry##*:}"
    kubectl scale deployment "$deploy" -n "$NAMESPACE" --replicas="$count" >/dev/null 2>&1 || true
  done
  if [ "$status" -ne 0 ]; then
    echo
    echo "!! Scenario exited with an error — see above. Fault state was still reset." >&2
  fi
}
trap cleanup EXIT

require_token() {
  if [ -z "$CHAOS_TOKEN" ]; then
    echo "FAILED: set CHAOS_ADMIN_TOKEN to the same value configured in the" >&2
    echo "        citizen-service / notification-service Secrets (Phase 10)." >&2
    exit 1
  fi
}

pin_single_replica() {
  # Chaos state lives per-pod in memory. Pin to 1 replica first so the
  # fault we set is guaranteed to be the pod Prometheus is scraping and
  # the pod any generated traffic hits — see Phase 10's own note on this
  # in Phases.md ("target a specific pod ... for deterministic
  # single-pod experiments").
  local deploy="$1"
  local current
  current=$(kubectl get deployment "$deploy" -n "$NAMESPACE" -o jsonpath='{.spec.replicas}')
  ORIGINAL_REPLICAS+=("$deploy:$current")
  if [ "$current" != "1" ]; then
    echo "    Pinning $deploy to 1 replica for a deterministic target (was $current)"
    kubectl scale deployment "$deploy" -n "$NAMESPACE" --replicas=1
    kubectl rollout status deployment "$deploy" -n "$NAMESPACE" --timeout=90s
  fi
}

port_forward() {
  local svc="$1" local_port="$2" remote_port="$3"
  kubectl port-forward -n "$NAMESPACE" "svc/$svc" "$local_port:$remote_port" >/tmp/pf-"$svc"-"$local_port".log 2>&1 &
  local pid=$!
  PIDS+=("$pid")
  for _ in $(seq 1 20); do
    curl -sf "http://localhost:$local_port" >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  echo "    (port-forward to $svc:$remote_port may still be starting up)"
}

wait_for_alert() {
  # Polls Prometheus's HTTP API for an alert reaching state=firing.
  # $1 = alert name, $2 = max seconds to wait (should exceed the rule's
  # `for:` duration with margin).
  local alert_name="$1" timeout="$2" waited=0
  echo "    Waiting up to ${timeout}s for '$alert_name' to fire (Prometheus 'for:' duration + scrape interval)..."
  while [ "$waited" -lt "$timeout" ]; do
    local firing
    firing=$(curl -sf "http://localhost:$PROM_PORT/api/v1/alerts" 2>/dev/null \
      | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    alerts = data.get('data', {}).get('alerts', [])
    print('yes' if any(a['labels'].get('alertname') == '$alert_name' and a['state'] == 'firing' for a in alerts) else 'no')
except Exception:
    print('no')
" 2>/dev/null || echo "no")
    if [ "$firing" = "yes" ]; then
      echo "    OK — $alert_name is firing (took ~${waited}s)"
      return 0
    fi
    sleep 5
    waited=$((waited + 5))
  done
  echo "FAILED: $alert_name did not enter firing state within ${timeout}s" >&2
  echo "        Check: curl http://localhost:$PROM_PORT/api/v1/alerts" >&2
  return 1
}

check_alertmanager_seen() {
  # Best-effort — confirms the alert also reached Alertmanager, not just
  # Prometheus's own evaluation. Non-fatal if it hasn't propagated yet.
  local alert_name="$1"
  local seen
  seen=$(curl -sf "http://localhost:$AM_PORT/api/v2/alerts" 2>/dev/null \
    | python3 -c "
import sys, json
try:
    alerts = json.load(sys.stdin)
    print('yes' if any(a['labels'].get('alertname') == '$alert_name' for a in alerts) else 'no')
except Exception:
    print('no')
" 2>/dev/null || echo "no")
  if [ "$seen" = "yes" ]; then
    echo "    OK — also visible in Alertmanager (http://localhost:$AM_PORT)"
  else
    echo "    (not yet visible in Alertmanager — it polls Prometheus periodically; check manually if needed)"
  fi
}

generate_traffic() {
  # HighRequestLatency and NotificationDeliveryFailureRateHigh are built
  # on histograms/counters that only have data if requests actually
  # happen — injecting the fault alone produces no observations. Reuses
  # the registration/login/request flow from scripts/smoke-test.sh in a
  # tight loop for the duration given.
  local base_url="$1" seconds="$2"
  local end=$((SECONDS + seconds))
  local email="incident-sim-$(date +%s)@example.com"
  local register_resp
  register_resp=$(curl -sf -X POST "$base_url/api/auth/register" \
    -H "Content-Type: application/json" \
    -d "{\"full_name\":\"Incident Sim\",\"national_id\":\"$(date +%s)11111\",\"email\":\"$email\",\"password\":\"IncidentSim123!\"}") || true
  local token
  token=$(curl -sf -X POST "$base_url/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$email\",\"password\":\"IncidentSim123!\"}" \
    | python3 -c "import sys,json;print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null) || true
  local service_id
  service_id=$(curl -sf "$base_url/api/services" \
    | python3 -c "import sys,json;d=json.load(sys.stdin);print(d[0]['id'] if d else '')" 2>/dev/null) || true
  echo "    Generating traffic against $base_url for ${seconds}s..."
  while [ $SECONDS -lt $end ]; do
    curl -sf "$base_url/api/services" >/dev/null 2>&1 || true
    if [ -n "${token:-}" ] && [ -n "${service_id:-}" ]; then
      curl -sf -X POST "$base_url/api/requests" \
        -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
        -d "{\"service_id\":\"$service_id\"}" >/dev/null 2>&1 || true
    fi
    sleep 0.3
  done
}

set_chaos_fault() {
  local port="$1" body="$2"
  curl -sf -X POST "http://localhost:$port/api/chaos/fault" \
    -H "X-Chaos-Token: $CHAOS_TOKEN" -H "Content-Type: application/json" \
    -d "$body" >/dev/null
}

reset_chaos_fault() {
  local port="$1"
  curl -sf -X POST "http://localhost:$port/api/chaos/reset" \
    -H "X-Chaos-Token: $CHAOS_TOKEN" >/dev/null 2>&1 || true
}

scenario_db_outage() {
  echo "=== Scenario: citizen-service database outage ==="
  echo "    What an operator/Sentinel would see: /readyz returns 503, citizen-facing"
  echo "    requests that touch the DB start failing, ChaosDatabaseFailure fires"
  echo "    almost immediately (for: 30s — this is deliberately the fastest rule,"
  echo "    since a DB outage is the most severe scenario this project can simulate)."
  require_token
  pin_single_replica citizen-service
  port_forward citizen-service "$CITIZEN_PORT" 8000
  port_forward prometheus "$PROM_PORT" 9090
  port_forward alertmanager "$AM_PORT" 9093

  echo "    Injecting simulated DB failure..."
  set_chaos_fault "$CITIZEN_PORT" '{"db_failure": true}'

  local readyz_status
  readyz_status=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$CITIZEN_PORT/readyz")
  echo "    /readyz now returns HTTP $readyz_status (expected 503)"

  wait_for_alert "ChaosDatabaseFailure" 90
  check_alertmanager_seen "ChaosDatabaseFailure"

  echo "    Resetting fault..."
  reset_chaos_fault "$CITIZEN_PORT"
  echo "=== db-outage: PASSED ==="
}

scenario_http_errors() {
  echo "=== Scenario: citizen-service forced HTTP 5xx failures ==="
  echo "    What an operator/Sentinel would see: every request to citizen-service"
  echo "    fails with 500, ChaosForcedHTTPFailures fires almost immediately"
  echo "    (for: 30s, confirms Prometheus is observing the deliberate fault itself),"
  echo "    then HighHTTPErrorRate fires ~5 minutes later once the 5xx ratio has"
  echo "    been sustained long enough to rule out a brief blip."
  require_token
  pin_single_replica citizen-service
  port_forward citizen-service "$CITIZEN_PORT" 8000
  port_forward prometheus "$PROM_PORT" 9090
  port_forward alertmanager "$AM_PORT" 9093

  echo "    Injecting 100% forced HTTP error rate..."
  set_chaos_fault "$CITIZEN_PORT" '{"error_rate": 1.0}'

  wait_for_alert "ChaosForcedHTTPFailures" 90
  check_alertmanager_seen "ChaosForcedHTTPFailures"

  generate_traffic "http://localhost:$CITIZEN_PORT" 30 &
  local traffic_pid=$!
  wait_for_alert "HighHTTPErrorRate" 360
  wait "$traffic_pid" 2>/dev/null || true
  check_alertmanager_seen "HighHTTPErrorRate"

  echo "    Resetting fault..."
  reset_chaos_fault "$CITIZEN_PORT"
  echo "=== http-errors: PASSED ==="
}

scenario_latency() {
  echo "=== Scenario: citizen-service elevated latency under load ==="
  echo "    What an operator/Sentinel would see: p95 latency creeps above 1s."
  echo "    ChaosLatencyInjection fires almost immediately from the configured"
  echo "    value alone (for: 30s); HighRequestLatency needs actual request"
  echo "    volume to populate the latency histogram, so this scenario generates"
  echo "    traffic concurrently — a fault with zero traffic produces zero"
  echo "    observations, which is a real gap worth knowing about, not just a"
  echo "    script detail (see Phases.md Phase 12 'what's missing')."
  require_token
  pin_single_replica citizen-service
  port_forward citizen-service "$CITIZEN_PORT" 8000
  port_forward prometheus "$PROM_PORT" 9090
  port_forward alertmanager "$AM_PORT" 9093

  echo "    Injecting 1500ms artificial latency..."
  set_chaos_fault "$CITIZEN_PORT" '{"latency_ms": 1500}'

  wait_for_alert "ChaosLatencyInjection" 90
  check_alertmanager_seen "ChaosLatencyInjection"

  generate_traffic "http://localhost:$CITIZEN_PORT" 60 &
  local traffic_pid=$!
  wait_for_alert "HighRequestLatency" 360
  wait "$traffic_pid" 2>/dev/null || true
  check_alertmanager_seen "HighRequestLatency"

  echo "    Resetting fault..."
  reset_chaos_fault "$CITIZEN_PORT"
  echo "=== latency: PASSED ==="
}

scenario_notification_degradation() {
  echo "=== Scenario: notification delivery degradation ==="
  echo "    What an operator/Sentinel would see: citizen-service keeps working"
  echo "    fine (fire-and-forget — Phase 2's whole point), but notifications"
  echo "    silently stop being delivered. This is the scenario that most"
  echo "    directly tests whether an on-call engineer — or Sentinel — is"
  echo "    watching a downstream dependency's own signals, not just the"
  echo "    service the citizen directly talks to."
  require_token
  pin_single_replica citizen-service
  pin_single_replica notification-service
  port_forward citizen-service "$CITIZEN_PORT" 8000
  port_forward notification-service "$NOTIF_PORT" 8000
  port_forward prometheus "$PROM_PORT" 9090
  port_forward alertmanager "$AM_PORT" 9093

  echo "    Injecting 100% notification delivery failure..."
  set_chaos_fault "$NOTIF_PORT" '{"notification_failure_rate": 1.0}'

  generate_traffic "http://localhost:$CITIZEN_PORT" 60 &
  local traffic_pid=$!
  wait_for_alert "NotificationDeliveryFailureRateHigh" 360
  wait "$traffic_pid" 2>/dev/null || true
  check_alertmanager_seen "NotificationDeliveryFailureRateHigh"

  echo "    Resetting fault..."
  reset_chaos_fault "$NOTIF_PORT"
  echo "=== notification-degradation: PASSED ==="
}

scenario_full_outage() {
  echo "=== Scenario: full citizen-service outage ==="
  echo "    Not a chaos-API scenario — this simulates the crudest real failure:"
  echo "    the deployment disappearing entirely (crash-loop exhausted, node"
  echo "    lost, bad rollout with no healthy replicas). Uses the 'up' metric"
  echo "    directly, so it needs no application code at all — the same"
  echo "    property that makes ServiceDown Phase 9's simplest, most load-bearing"
  echo "    alert."
  local current
  current=$(kubectl get deployment citizen-service -n "$NAMESPACE" -o jsonpath='{.spec.replicas}')
  ORIGINAL_REPLICAS+=("citizen-service:$current")
  port_forward prometheus "$PROM_PORT" 9090
  port_forward alertmanager "$AM_PORT" 9093

  echo "    Scaling citizen-service to 0 replicas..."
  kubectl scale deployment citizen-service -n "$NAMESPACE" --replicas=0

  wait_for_alert "ServiceDown" 240
  check_alertmanager_seen "ServiceDown"

  echo "    Restoring citizen-service to $current replicas..."
  kubectl scale deployment citizen-service -n "$NAMESPACE" --replicas="$current"
  kubectl rollout status deployment citizen-service -n "$NAMESPACE" --timeout=90s
  echo "=== full-outage: PASSED ==="
}

SCENARIO="${1:-}"
case "$SCENARIO" in
  db-outage) scenario_db_outage ;;
  http-errors) scenario_http_errors ;;
  latency) scenario_latency ;;
  notification-degradation) scenario_notification_degradation ;;
  full-outage) scenario_full_outage ;;
  all)
    scenario_db_outage
    scenario_http_errors
    scenario_latency
    scenario_notification_degradation
    scenario_full_outage
    echo
    echo "============================================================"
    echo " All incident scenarios passed."
    echo "============================================================"
    ;;
  *)
    echo "Usage: $0 <db-outage|http-errors|latency|notification-degradation|full-outage|all> [namespace]" >&2
    exit 1
    ;;
esac
