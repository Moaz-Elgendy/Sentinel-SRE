#!/bin/bash
# Phase 12 — end-to-end incident simulations.
#
# Drives a small set of named, repeatable incidents through the Phase 10
# chaos control API, then confirms Phase 9's stack actually catches each
# one: the right Prometheus alert enters "firing" within its `for:`
# window, reaches Alertmanager, and stays active long enough for Sentinel to
# investigate, remediate and validate the recovery.
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
#            | full-outage | high-cpu | memory-leak | crashloop
#            | bad-deployment | all
#
# Eight scenarios, in three families:
#   * chaos-API application faults (db-outage, http-errors, latency,
#     notification-degradation, high-cpu, memory-leak) — injected in-process
#     through the Phase 10 control plane, reversible with a single
#     POST /api/chaos/reset;
#   * platform faults (full-outage, crashloop) — driven purely through
#     kubectl, needing no application cooperation at all, which is exactly
#     what makes them the most trustworthy of the eight;
#   * release faults (bad-deployment) — a bad rollout that only a rollback
#     fixes. This one deliberately LEAVES THE SYSTEM BROKEN, see its own
#     notes below.
#
# Requires: kubectl context pointed at the cluster, the chaos control
# plane enabled (CHAOS_MODE=true + CHAOS_ADMIN_TOKEN set in both
# services' Secrets — see Phases.md Phase 10), and the Phase 9 monitoring
# stack deployed.
#
# Each scenario port-forwards what it needs, runs, verifies, cleans up
# its port-forwards, and holds injected faults until Sentinel has had time to
# detect and remediate them. A timeout is a scenario failure; cleanup then
# resets the fault as a safety net.
# (see the trap below). Chaos state is per-pod and in-memory (Phase 10),
# so scenarios that need a deterministic single target first scale the
# relevant Deployment to 1 replica, and restore the original replica
# count afterward.
set -euo pipefail

NAMESPACE="${2:-citizen-portal}"
ALERTMANAGER_PROPAGATION_TIMEOUT="${ALERTMANAGER_PROPAGATION_TIMEOUT:-90}"
SENTINEL_REMEDIATION_TIMEOUT="${SENTINEL_REMEDIATION_TIMEOUT:-300}"
SCENARIO_STARTED_EPOCH=0
# Read at top level, not inside the function: inside a shell function $3 is
# the *function's* third argument, not the script's.
AUTO_ROLLBACK="${3:-${BAD_DEPLOYMENT_AUTO_ROLLBACK:-false}}"
CHAOS_TOKEN="${CHAOS_ADMIN_TOKEN:-}"
PROM_PORT=9090
AM_PORT=9093
CITIZEN_PORT=18000
NOTIF_PORT=18001

PIDS=()
ORIGINAL_REPLICAS=()
# Deployments whose Pod template this run has deliberately broken and which
# MUST be rolled back on exit — including on Ctrl-C or a mid-scenario
# failure. A crash-loop we walked away from is not a demo, it's an outage.
# Scenarios add themselves here before breaking anything and remove
# themselves once they have recovered the Deployment on the happy path.
# bad-deployment deliberately does NOT register here: leaving the bad
# release in place is its entire point.
PENDING_ROLLBACKS=()

cleanup() {
  local status=$?
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" >/dev/null 2>&1 || true
  done
  # Named differently from the loop variable below so this doesn't leak a
  # global that the ORIGINAL_REPLICAS loop then declares `local`.
  for pending in "${PENDING_ROLLBACKS[@]:-}"; do
    [ -z "$pending" ] && continue
    echo "    Rolling back $pending (registered for automatic rollback)..." >&2
    kubectl rollout undo deployment "$pending" -n "$NAMESPACE" >/dev/null 2>&1 || true
    kubectl rollout status deployment "$pending" -n "$NAMESPACE" --timeout=120s >/dev/null 2>&1 || true
  done
  for entry in "${ORIGINAL_REPLICAS[@]:-}"; do
    [ -z "$entry" ] && continue
    local deploy="${entry%%:*}"
    local count="${entry##*:}"
    kubectl scale deployment "$deploy" -n "$NAMESPACE" --replicas="$count" >/dev/null 2>&1 || true
  done
  if [ -n "${CHAOS_TOKEN:-}" ]; then
    reset_chaos_fault "$CITIZEN_PORT"
    reset_chaos_fault "$NOTIF_PORT"
  fi
  if [ "$status" -ne 0 ]; then
    echo
    echo "!! Scenario exited with an error — see above. Fault state was still reset." >&2
  fi
}
# INT/TERM as well as EXIT: bash does not reliably run an EXIT trap when the
# shell is killed by an untrapped signal, and Ctrl-C during the crashloop
# scenario is precisely when we most need the rollback to happen.
trap cleanup EXIT INT TERM

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
      | SCENARIO_STARTED_EPOCH="$SCENARIO_STARTED_EPOCH" ALERT_NAME="$alert_name" python3 -c "
import json, os, sys
from datetime import datetime
try:
    data = json.load(sys.stdin)
    start = int(os.environ['SCENARIO_STARTED_EPOCH'])
    name = os.environ['ALERT_NAME']
    for a in data.get('data', {}).get('alerts', []):
        if a.get('labels', {}).get('alertname') != name or a.get('state') != 'firing':
            continue
        active_at = a.get('activeAt')
        if not active_at:
            continue
        active_epoch = datetime.fromisoformat(active_at.replace('Z', '+00:00')).timestamp()
        if active_epoch >= start - 5:
            print('yes')
            break
    else:
        print('no')
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

wait_for_alertmanager() {
  # Prometheus firing is NOT enough for an end-to-end Sentinel scenario.
  # Alertmanager has its own group_wait, and Sentinel receives the alert only
  # from Alertmanager's webhook. Keep the fault active until the firing alert
  # is actually visible here.
  local alert_name="$1" timeout="${2:-$ALERTMANAGER_PROPAGATION_TIMEOUT}" waited=0
  echo "    Waiting up to ${timeout}s for '$alert_name' to reach Alertmanager/Sentinel..."
  while [ "$waited" -lt "$timeout" ]; do
    local seen
    seen=$(curl -sf "http://localhost:$AM_PORT/api/v2/alerts" 2>/dev/null \
      | SCENARIO_STARTED_EPOCH="$SCENARIO_STARTED_EPOCH" ALERT_NAME="$alert_name" python3 -c "
import json, os, sys
from datetime import datetime
try:
    alerts = json.load(sys.stdin)
    start = int(os.environ['SCENARIO_STARTED_EPOCH'])
    name = os.environ['ALERT_NAME']
    for a in alerts:
        if a.get('labels', {}).get('alertname') != name:
            continue
        if (a.get('status') or {}).get('state') != 'active':
            continue
        starts_at = a.get('startsAt')
        if not starts_at:
            continue
        starts_epoch = datetime.fromisoformat(starts_at.replace('Z', '+00:00')).timestamp()
        if starts_epoch >= start - 5:
            print('yes')
            break
    else:
        print('no')
except Exception:
    print('no')
" 2>/dev/null || echo "no")
    if [ "$seen" = "yes" ]; then
      echo "    OK — $alert_name is active in Alertmanager (Sentinel webhook can now receive it)"
      return 0
    fi
    sleep 5
    waited=$((waited + 5))
  done
  echo "FAILED: $alert_name reached Prometheus but did not become active in Alertmanager within ${timeout}s" >&2
  echo "        Check: curl http://localhost:$AM_PORT/api/v2/alerts" >&2
  return 1
}

wait_for_alert_inactive() {
  local alert_name="$1" timeout="${2:-180}" waited=0
  echo "    Waiting up to ${timeout}s for '$alert_name' to clear in Prometheus and Alertmanager..."
  while [ "$waited" -lt "$timeout" ]; do
    local prom_response am_response prom_active am_active
    prom_response=$(curl -sf "http://localhost:$PROM_PORT/api/v1/alerts" 2>/dev/null || true)
    am_response=$(curl -sf "http://localhost:$AM_PORT/api/v2/alerts" 2>/dev/null || true)
    if [ -n "$prom_response" ] && [ -n "$am_response" ]; then
      prom_active=$(printf '%s' "$prom_response" | ALERT_NAME="$alert_name" python3 -c '
import json, os, sys
name = os.environ["ALERT_NAME"]
try:
    alerts = json.load(sys.stdin).get("data", {}).get("alerts", [])
    print("yes" if any(a.get("labels", {}).get("alertname") == name for a in alerts) else "no")
except Exception:
    print("unknown")
' 2>/dev/null || echo "unknown")
      am_active=$(printf '%s' "$am_response" | ALERT_NAME="$alert_name" python3 -c '
import json, os, sys
name = os.environ["ALERT_NAME"]
try:
    alerts = json.load(sys.stdin)
    print("yes" if any(a.get("labels", {}).get("alertname") == name for a in alerts) else "no")
except Exception:
    print("unknown")
' 2>/dev/null || echo "unknown")
      if [ "$prom_active" = "no" ] && [ "$am_active" = "no" ]; then
        echo "    OK — $alert_name is inactive in Prometheus and Alertmanager"
        return 0
      fi
    fi
    sleep 5
    waited=$((waited + 5))
  done
  echo "FAILED: $alert_name did not clear from Prometheus and Alertmanager within ${timeout}s" >&2
  return 1
}

# Backward-compatible informational helper used by platform-only scenarios.
# Application-fault scenarios MUST use wait_for_alertmanager instead.
check_alertmanager_seen() {
  wait_for_alertmanager "$1" "${2:-$ALERTMANAGER_PROPAGATION_TIMEOUT}" || true
}

unregister_rollback() {
  # Called on the happy path once a scenario has recovered a Deployment
  # itself, so cleanup() doesn't issue a second, pointless `rollout undo`
  # that would take the Deployment back to a revision nobody asked for.
  local target="$1" remaining=()
  for deploy in "${PENDING_ROLLBACKS[@]:-}"; do
    [ -z "$deploy" ] && continue
    [ "$deploy" = "$target" ] && continue
    remaining+=("$deploy")
  done
  PENDING_ROLLBACKS=("${remaining[@]:-}")
}

run_safe_suite() {
  local scenario failures=() passed=()
  local scenarios=(
    db-outage
    http-errors
    latency
    notification-degradation
    high-cpu
    memory-leak
    crashloop
    full-outage
  )

  for scenario in "${scenarios[@]}"; do
    echo
    echo "============================================================"
    echo " Suite scenario: $scenario"
    echo "============================================================"
    if "$0" "$scenario" "$NAMESPACE" "$AUTO_ROLLBACK"; then
      passed+=("$scenario")
    else
      local rc=$?
      failures+=("$scenario:$rc")
      echo "!! $scenario: FAILED (exit $rc); continuing with remaining independent scenarios." >&2
    fi
  done

  echo
  echo "============================================================"
  echo " Incident scenario suite summary"
  echo " Passed: ${passed[*]:-(none)}"
  echo " Failed: ${failures[*]:-(none)}"
  echo " (bad-deployment excluded from 'all' by design - it leaves the"
  echo "  system broken on purpose. Run it explicitly.)"
  echo "============================================================"

  [ "${#failures[@]}" -eq 0 ]
}

deployment_revision() {
  # The revision counter Deployments keep in an annotation. Comparing it
  # before/after is how we prove a rollout actually happened rather than
  # trusting that `kubectl set env` changed something.
  kubectl get deployment "$1" -n "$NAMESPACE" \
    -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}'
}

wait_for_waiting_reason() {
  # Polls the Pods of a Deployment for a container stuck in a specific
  # waiting reason (e.g. CrashLoopBackOff). This is the same field the
  # kubelet reports and `kubectl get pods` renders in its STATUS column, so
  # asserting on it is asserting on exactly what an operator would see.
  # $1 = deployment/app label, $2 = reason, $3 = max seconds.
  local deploy="$1" reason="$2" timeout="$3" waited=0
  echo "    Waiting up to ${timeout}s for a $deploy container to report '$reason'..."
  while [ "$waited" -lt "$timeout" ]; do
    local reasons
    reasons=$(kubectl get pods -n "$NAMESPACE" -l "app=$deploy" \
      -o jsonpath='{range .items[*]}{range .status.containerStatuses[*]}{.state.waiting.reason}{"\n"}{end}{end}' 2>/dev/null || true)
    if echo "$reasons" | grep -q "^$reason$"; then
      echo "    OK — $deploy is in $reason (took ~${waited}s)"
      return 0
    fi
    sleep 5
    waited=$((waited + 5))
  done
  echo "FAILED: no $deploy container reached '$reason' within ${timeout}s" >&2
  echo "        Check: kubectl get pods -n $NAMESPACE -l app=$deploy" >&2
  return 1
}

wait_for_no_ready_replicas() {
  # Inverse of `kubectl rollout status`: waits for a Deployment to have zero
  # ready replicas, i.e. for the broken release to have actually taken the
  # service down rather than being quietly held back by maxUnavailable.
  local deploy="$1" timeout="$2" waited=0
  echo "    Waiting up to ${timeout}s for $deploy to have no ready replicas..."
  while [ "$waited" -lt "$timeout" ]; do
    local ready
    ready=$(kubectl get deployment "$deploy" -n "$NAMESPACE" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)
    if [ -z "$ready" ] || [ "$ready" = "0" ]; then
      echo "    OK — $deploy has no ready replicas (took ~${waited}s)"
      return 0
    fi
    sleep 5
    waited=$((waited + 5))
  done
  echo "FAILED: $deploy still had ready replicas after ${timeout}s — the broken" >&2
  echo "        release may not be rolling out (check maxUnavailable / probes)." >&2
  return 1
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
  SCENARIO_STARTED_EPOCH=$(date +%s)
  curl -sf -X POST "http://localhost:$port/api/chaos/fault" \
    -H "X-Chaos-Token: $CHAOS_TOKEN" -H "Content-Type: application/json" \
    -d "$body" >/dev/null
}

reset_chaos_fault() {
  local port="$1"
  curl -sf -X POST "http://localhost:$port/api/chaos/reset" \
    -H "X-Chaos-Token: $CHAOS_TOKEN" >/dev/null 2>&1 || true
}

wait_for_chaos_clear() {
  local service="$1" port="$2" state_field="$3"
  local timeout="${4:-$SENTINEL_REMEDIATION_TIMEOUT}" waited=0

  case "$state_field" in
    error_rate|latency_ms|db_failure|cpu_burn|memory_leak_mb|notification_failure_rate) ;;
    *)
      echo "FAILED: unsupported chaos status field '$state_field'" >&2
      return 2
      ;;
  esac

  echo "    Waiting up to ${timeout}s for Sentinel to clear $service $state_field..."
  while [ "$waited" -lt "$timeout" ]; do
    local status state
    status=$(curl -fsS --max-time 5 "http://localhost:$port/api/chaos/status" \
      -H "X-Chaos-Token: $CHAOS_TOKEN" 2>/dev/null || true)
    state=$(printf '%s' "$status" \
      | CHAOS_STATE_FIELD="$state_field" python3 -c '
import json
import os
import sys

try:
    body = json.load(sys.stdin)
    value = body.get(os.environ["CHAOS_STATE_FIELD"])
    if isinstance(value, bool):
        print("clear" if value is False else "active")
    elif isinstance(value, (int, float)):
        print("clear" if value == 0 else "active")
    else:
        print("unknown")
except Exception:
    print("unknown")
' 2>/dev/null || echo "unknown")

    if [ "$state" = "clear" ]; then
      echo "    OK — $service $state_field is clear (took ~${waited}s)"
      return 0
    fi

    sleep 5
    waited=$((waited + 5))
  done

  echo "FAILED: Sentinel did not clear $service $state_field within ${timeout}s" >&2
  echo "        Last chaos status response: ${status:-<unreachable>}" >&2
  return 1
}

rollback_citizen_service() {
  # Shared by both the crashloop and bad-deployment detections below.
  # Deliberately NOT allowed to take the whole recovery down with it (see
  # the call sites): `set -e` would otherwise abort scenario_reset_all the
  # instant one `kubectl rollout status` timed out, silently skipping every
  # check after it — e.g. leaving a full-outage scale-to-zero in place
  # forever because the crashloop check above it happened to time out
  # first. A single rollback attempt can also land on another bad revision
  # if bad-deployment was run more than once without a reset in between
  # (`rollout undo` with no target only ever steps back one revision), so
  # this retries once before giving up.
  local reason="$1" attempt
  for attempt in 1 2; do
    if kubectl rollout undo deployment citizen-service -n "$NAMESPACE" >/dev/null 2>&1 \
      && kubectl rollout status deployment citizen-service -n "$NAMESPACE" --timeout=240s >/dev/null 2>&1; then
      echo "    OK — citizen-service rolled back and ready ($reason, attempt $attempt)"
      return 0
    fi
    echo "    Rollback attempt $attempt for '$reason' did not reach Ready within 240s..." >&2
  done
  echo "FAILED: could not roll citizen-service back to a healthy revision ($reason)." >&2
  echo "        Check: kubectl rollout history deployment citizen-service -n $NAMESPACE" >&2
  echo "               kubectl get pods -n $NAMESPACE -l app=citizen-service" >&2
  return 1
}

scenario_reset_all() {
  echo "=== Recovery: reset active chaos scenarios ==="
  echo "    Clearing reachable in-process faults and undoing only known"
  echo "    incident-scenarios.sh platform/release mutations."
  require_token

  # These port-forwards are best-effort: a platform scenario may have taken
  # the application down, but the kubectl recovery checks below still run.
  port_forward citizen-service "$CITIZEN_PORT" 8000
  port_forward notification-service "$NOTIF_PORT" 8000
  reset_chaos_fault "$CITIZEN_PORT"
  reset_chaos_fault "$NOTIF_PORT"

  # Each check below is independent and best-effort: one taking longer than
  # its own timeout must not stop the others from running (that used to be
  # exactly what happened, since an unguarded `kubectl rollout status`
  # failing under `set -e` aborted the whole function on the spot). Track
  # failures and keep going, then report honestly at the end instead of
  # always printing "recovery command completed" even when it wasn't.
  local failures=()

  local command
  command=$(kubectl get deployment citizen-service -n "$NAMESPACE" \
    -o jsonpath='{.spec.template.spec.containers[0].command[*]}' 2>/dev/null || true)
  if echo "$command" | grep -q 'incident-scenarios.sh.*crashloop'; then
    echo "    Detected the scenario crashloop command; rolling back citizen-service..."
    rollback_citizen_service "crashloop" || failures+=("crashloop")
  fi

  local env_values
  env_values=$(kubectl get deployment citizen-service -n "$NAMESPACE" \
    -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' \
    2>/dev/null || true)
  if echo "$env_values" | grep -q 'DATABASE_HOST=citizen-postgres-does-not-exist.invalid'; then
    echo "    Detected the scenario bad-deployment database host; rolling back citizen-service..."
    rollback_citizen_service "bad-deployment" || failures+=("bad-deployment")
  fi

  local replicas
  replicas=$(kubectl get deployment citizen-service -n "$NAMESPACE" \
    -o jsonpath='{.spec.replicas}' 2>/dev/null || true)
  if [ "$replicas" = "0" ]; then
    echo "    Detected the scenario full-outage scale-to-zero; restoring one replica..."
    if kubectl scale deployment citizen-service -n "$NAMESPACE" --replicas=1 >/dev/null 2>&1 \
      && kubectl rollout status deployment citizen-service -n "$NAMESPACE" --timeout=180s >/dev/null 2>&1; then
      echo "    OK — citizen-service restored to 1 replica and ready"
    else
      echo "FAILED: could not restore citizen-service after full-outage scale-to-zero." >&2
      failures+=("full-outage")
    fi
  fi

  if [ "${#failures[@]}" -eq 0 ]; then
    echo "=== reset-all: recovery completed, no leftover mutations required manual attention ==="
  else
    echo "!! reset-all: recovery ran but could not fix: ${failures[*]} — see FAILED lines above." >&2
    echo "=== reset-all: recovery completed with unresolved issues ===" >&2
    return 1
  fi
}

scenario_db_outage() {
  echo "=== Scenario: citizen-service database outage ==="
  echo "    What an operator/Sentinel would see: /readyz returns 503, citizen-facing"
  echo "    requests that touch the DB start failing, ChaosDatabaseFailure fires"
  echo "    after its 30s Prometheus 'for:' window, then Alertmanager delivers the"
  echo "    firing webhook and Sentinel investigates/resets the injected fault."
  require_token
  pin_single_replica citizen-service
  port_forward citizen-service "$CITIZEN_PORT" 8000

  echo "    Injecting simulated DB failure..."
  set_chaos_fault "$CITIZEN_PORT" '{"db_failure": true}'

  local readyz_status
  readyz_status=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$CITIZEN_PORT/readyz")
  echo "    /readyz now returns HTTP $readyz_status (expected 503)"

  port_forward prometheus "$PROM_PORT" 9090
  port_forward alertmanager "$AM_PORT" 9093

  wait_for_alert "ChaosDatabaseFailure" 90
  wait_for_alertmanager "ChaosDatabaseFailure"
  wait_for_chaos_clear citizen-service "$CITIZEN_PORT" db_failure

  echo "=== db-outage: PASSED — Sentinel detected and cleared the injected DB fault ==="
}

scenario_http_errors() {
  echo "=== Scenario: citizen-service forced HTTP 5xx failures ==="
  echo "    The deliberate 5xx fault stays enabled while Sentinel detects and"
  echo "    remediates it. We verify Alertmanager delivery before waiting for"
  echo "    Sentinel's chaos reset rather than resetting the fault ourselves."
  require_token
  pin_single_replica citizen-service
  port_forward citizen-service "$CITIZEN_PORT" 8000

  echo "    Injecting 100% forced HTTP error rate..."
  set_chaos_fault "$CITIZEN_PORT" '{"error_rate": 1.0}'

  port_forward prometheus "$PROM_PORT" 9090
  port_forward alertmanager "$AM_PORT" 9093

  wait_for_alert "ChaosForcedHTTPFailures" 90
  wait_for_alertmanager "ChaosForcedHTTPFailures"

  # HighHTTPErrorRate is a slower, aggregate signal. It is useful observability
  # but must not block the scenario from exercising the faster, deterministic
  # chaos alert -> Sentinel remediation path. Keep traffic active while the
  # fault is enabled, but wait on the chaos gauge being cleared by Sentinel.
  generate_traffic "http://localhost:$CITIZEN_PORT" 180 &
  local traffic_pid=$!
  wait_for_chaos_clear citizen-service "$CITIZEN_PORT" error_rate
  wait "$traffic_pid" 2>/dev/null || true
  wait_for_alert_inactive "ChaosForcedHTTPFailures"

  echo "=== http-errors: PASSED — Sentinel detected and cleared the injected HTTP fault ==="
}

scenario_latency() {
  echo "=== Scenario: citizen-service elevated latency under load ==="
  echo "    ChaosLatencyInjection fires after 30s from the configured value."
  echo "    Traffic runs concurrently so the normal latency signal can also be"
  echo "    observed, but the scenario waits for Sentinel to clear the injected"
  echo "    latency rather than resetting it as soon as Prometheus fires."
  require_token
  pin_single_replica citizen-service
  port_forward citizen-service "$CITIZEN_PORT" 8000

  echo "    Injecting 1500ms artificial latency..."
  set_chaos_fault "$CITIZEN_PORT" '{"latency_ms": 1500}'

  port_forward prometheus "$PROM_PORT" 9090
  port_forward alertmanager "$AM_PORT" 9093

  wait_for_alert "ChaosLatencyInjection" 90
  wait_for_alertmanager "ChaosLatencyInjection"

  generate_traffic "http://localhost:$CITIZEN_PORT" 180 &
  local traffic_pid=$!
  wait_for_chaos_clear citizen-service "$CITIZEN_PORT" latency_ms
  wait "$traffic_pid" 2>/dev/null || true

  echo "=== latency: PASSED — Sentinel detected and cleared the injected latency ==="
}

scenario_notification_degradation() {
  echo "=== Scenario: notification delivery degradation ==="
  echo "    Notification failures are injected while citizen-service traffic"
  echo "    continues. Alertmanager delivery is verified, then the fault remains"
  echo "    active until Sentinel clears the notification chaos state."
  require_token
  pin_single_replica citizen-service
  pin_single_replica notification-service
  port_forward notification-service "$NOTIF_PORT" 8000

  echo "    Injecting 100% notification delivery failure..."
  set_chaos_fault "$NOTIF_PORT" '{"notification_failure_rate": 1.0}'

  port_forward citizen-service "$CITIZEN_PORT" 8000
  port_forward prometheus "$PROM_PORT" 9090
  port_forward alertmanager "$AM_PORT" 9093

  # This alert has a 5m Prometheus 'for:' window, so keep traffic alive and
  # do not reset the fault when the first alert merely reaches Prometheus.
  generate_traffic "http://localhost:$CITIZEN_PORT" 420 &
  local traffic_pid=$!
  wait_for_alert "NotificationDeliveryFailureRateHigh" 420
  wait_for_alertmanager "NotificationDeliveryFailureRateHigh"
  wait_for_chaos_clear notification-service "$NOTIF_PORT" notification_failure_rate
  wait "$traffic_pid" 2>/dev/null || true

  echo "=== notification-degradation: PASSED — Sentinel detected and cleared the injected notification fault ==="
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

  echo "    Scaling citizen-service to 0 replicas..."
  kubectl scale deployment citizen-service -n "$NAMESPACE" --replicas=0

  # Prometheus/Alertmanager are only needed for the verification below, not
  # for the fault itself — opened after the scale so the outage takes effect
  # as soon as possible once this scenario is triggered.
  port_forward prometheus "$PROM_PORT" 9090
  port_forward alertmanager "$AM_PORT" 9093

  wait_for_alert "ServiceDown" 240
  check_alertmanager_seen "ServiceDown"

  echo "    Restoring citizen-service to $current replicas..."
  kubectl scale deployment citizen-service -n "$NAMESPACE" --replicas="$current"
  kubectl rollout status deployment citizen-service -n "$NAMESPACE" --timeout=90s
  echo "=== full-outage: PASSED ==="
}

scenario_high_cpu() {
  echo "=== Scenario: citizen-service CPU exhaustion ==="
  echo "    The CPU burn remains enabled after Prometheus fires so Sentinel can"
  echo "    investigate, choose its remediation, and validate recovery."
  require_token
  pin_single_replica citizen-service
  port_forward citizen-service "$CITIZEN_PORT" 8000

  echo "    Enabling the CPU burn worker..."
  set_chaos_fault "$CITIZEN_PORT" '{"cpu_burn": true}'

  local healthz_status
  healthz_status=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$CITIZEN_PORT/healthz")
  echo "    /healthz now returns HTTP $healthz_status while burning (expected 200)"

  port_forward prometheus "$PROM_PORT" 9090
  port_forward alertmanager "$AM_PORT" 9093

  wait_for_alert "HighCPUUsage" 420
  wait_for_alertmanager "HighCPUUsage"
  wait_for_chaos_clear citizen-service "$CITIZEN_PORT" cpu_burn

  echo "=== high-cpu: PASSED — Sentinel detected and cleared the CPU fault ==="
}

scenario_memory_leak() {
  local leak_mb=64
  echo "=== Scenario: citizen-service memory leak (${leak_mb} MiB retained) ==="
  echo "    The leak remains active after MemoryLeakSuspected fires so Sentinel"
  echo "    has time to investigate and perform its configured remediation."
  require_token
  pin_single_replica citizen-service
  port_forward citizen-service "$CITIZEN_PORT" 8000

  echo "    Retaining ${leak_mb} MiB in the leak buffer..."
  set_chaos_fault "$CITIZEN_PORT" "{\"memory_leak_mb\": $leak_mb}"

  port_forward prometheus "$PROM_PORT" 9090
  port_forward alertmanager "$AM_PORT" 9093

  wait_for_alert "MemoryLeakSuspected" 420
  wait_for_alertmanager "MemoryLeakSuspected"
  wait_for_chaos_clear citizen-service "$CITIZEN_PORT" memory_leak_mb

  echo "=== memory-leak: PASSED — Sentinel detected and cleared/remediated the memory fault ==="
}

scenario_crashloop() {
  echo "=== Scenario: citizen-service CrashLoopBackOff ==="
  echo "    Deliberately NOT a chaos-API scenario: the whole point is a pod that"
  echo "    dies before the application — and therefore before the chaos control"
  echo "    plane — exists at all. This is the failure mode that no amount of"
  echo "    in-process instrumentation can report on itself, so it is the one"
  echo "    that proves the platform-level signals (kube-state / pod phase /"
  echo "    ServiceDown) are doing real work."
  echo "    Mechanism: override the container's command with one that exits"
  echo "    non-zero immediately. Reversible with a single 'kubectl rollout"
  echo "    undo', and registered for automatic rollback even if this script is"
  echo "    interrupted."
  local container
  container=$(kubectl get deployment citizen-service -n "$NAMESPACE" \
    -o jsonpath='{.spec.template.spec.containers[0].name}')
  # No Prometheus/Alertmanager port-forward here: this scenario verifies via
  # wait_for_waiting_reason (a direct kubectl poll), never wait_for_alert or
  # check_alertmanager_seen, so those tunnels would only add setup latency
  # before the actual fault below with nothing to show for it.

  local before_revision
  before_revision=$(deployment_revision citizen-service)
  echo "    Current revision: $before_revision"

  echo "    Patching container '$container' to exit 1 on startup..."
  PENDING_ROLLBACKS+=("citizen-service")
  kubectl patch deployment citizen-service -n "$NAMESPACE" --type=strategic -p \
    "{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"$container\",\"command\":[\"/bin/sh\",\"-c\",\"echo 'simulated startup failure (incident-scenarios.sh crashloop)'; exit 1\"]}]}}}}"

  # Not rollout status: we *want* this rollout to fail. CrashLoopBackOff only
  # appears after the kubelet's back-off kicks in on the second restart, so
  # this needs more than a few seconds.
  wait_for_waiting_reason citizen-service CrashLoopBackOff 240

  echo "    Restart counts now:"
  kubectl get pods -n "$NAMESPACE" -l app=citizen-service \
    -o custom-columns='POD:.metadata.name,RESTARTS:.status.containerStatuses[0].restartCount,STATUS:.status.containerStatuses[0].state.waiting.reason' \
    2>/dev/null || true

  echo "    Recovering with 'kubectl rollout undo'..."
  kubectl rollout undo deployment citizen-service -n "$NAMESPACE"
  kubectl rollout status deployment citizen-service -n "$NAMESPACE" --timeout=180s
  unregister_rollback citizen-service

  local after_revision
  after_revision=$(deployment_revision citizen-service)
  echo "    Recovered on revision $after_revision (was $before_revision before the break)"
  local ready
  ready=$(kubectl get deployment citizen-service -n "$NAMESPACE" -o jsonpath='{.status.readyReplicas}')
  if [ -z "$ready" ] || [ "$ready" = "0" ]; then
    echo "FAILED: citizen-service has no ready replicas after the rollback" >&2
    return 1
  fi
  echo "    OK — $ready ready replica(s) after rollback"
  echo "=== crashloop: PASSED ==="
}

scenario_bad_deployment() {
  # Auto-rollback is opt-in via a third positional arg or the env var, so the
  # same scenario serves two very different purposes:
  #   * default (agent mode): break it and WALK AWAY. Sentinel is supposed to
  #     notice and roll back by itself; a script that tidies up after itself
  #     would be grading its own homework.
  #   * auto-rollback (smoke-test mode): prove the mechanics work in CI
  #     without leaving a broken cluster behind for the next job.
  local auto_rollback="$AUTO_ROLLBACK"
  echo "=== Scenario: bad citizen-service release (rollback required) ==="
  echo "    What an operator/Sentinel would see: a rollout that completes as far"
  echo "    as Kubernetes is concerned but whose new pods never pass /readyz,"
  echo "    because the app now points at a database host that does not exist."
  echo "    The only real fix is a rollback — no amount of restarting helps,"
  echo "    which is exactly what distinguishes a bad release from a transient"
  echo "    fault, and exactly the judgement call this scenario is here to test."
  # No Prometheus/Alertmanager port-forward here: this scenario verifies via
  # wait_for_no_ready_replicas (a direct kubectl poll), never wait_for_alert
  # or check_alertmanager_seen, so those tunnels would only add setup latency
  # before the actual fault below with nothing to show for it.

  local before_revision
  before_revision=$(deployment_revision citizen-service)
  echo "    Good revision (the rollback target): $before_revision"

  echo "    Pointing DATABASE_HOST at a nonexistent host..."
  kubectl set env deployment/citizen-service -n "$NAMESPACE" \
    DATABASE_HOST=citizen-postgres-does-not-exist.invalid

  local after_revision
  after_revision=$(deployment_revision citizen-service)
  if [ "$after_revision" = "$before_revision" ]; then
    echo "FAILED: revision did not advance ($before_revision) — the bad change" >&2
    echo "        never became a rollout, so there is nothing to roll back." >&2
    return 1
  fi
  echo "    OK — new revision $after_revision created (rollback target remains $before_revision)"
  echo "    Deployment history:"
  kubectl rollout history deployment citizen-service -n "$NAMESPACE" || true

  wait_for_no_ready_replicas citizen-service 240

  if [ "$auto_rollback" = "true" ]; then
    echo "    Auto-rollback requested — rolling back instead of leaving the"
    echo "    incident open (smoke-test mode, not agent mode)."
    kubectl rollout undo deployment citizen-service -n "$NAMESPACE"
    kubectl rollout status deployment citizen-service -n "$NAMESPACE" --timeout=180s
    echo "    Recovered on revision $(deployment_revision citizen-service)"
  else
    echo
    echo "    >>> THE SYSTEM IS INTENTIONALLY LEFT BROKEN. <<<"
    echo "    This is the scenario an autonomous agent (Sentinel) is expected to"
    echo "    detect and remediate on its own, without being told what happened."
    echo "    Nothing below this line will fix it — that is the test."
    echo
    echo "    Expected agent action:"
    echo "      kubectl rollout undo deployment citizen-service -n $NAMESPACE"
    echo "    To recover by hand, run exactly that. To run this scenario as a"
    echo "    non-agent smoke test instead, pass 'true' as the third argument or"
    echo "    set BAD_DEPLOYMENT_AUTO_ROLLBACK=true."
    echo
  fi
  echo "=== bad-deployment: PASSED ==="
}

SCENARIO="${1:-}"
case "$SCENARIO" in
  db-outage) scenario_db_outage ;;
  http-errors) scenario_http_errors ;;
  latency) scenario_latency ;;
  notification-degradation) scenario_notification_degradation ;;
  full-outage) scenario_full_outage ;;
  high-cpu) scenario_high_cpu ;;
  memory-leak) scenario_memory_leak ;;
  crashloop) scenario_crashloop ;;
  bad-deployment) scenario_bad_deployment ;;
  reset-all) scenario_reset_all ;;
  all)
    # 'all' runs the seven scenarios that leave the cluster the way they
    # found it, in roughly increasing order of disruption. bad-deployment is
    # deliberately EXCLUDED: it is the one scenario whose contract is to walk
    # away from a broken system, so including it would mean 'all' ends with
    # citizen-service down and every subsequent run of any scenario failing
    # for reasons that have nothing to do with the scenario. Anything that
    # makes a test suite non-repeatable does not belong in the "run
    # everything" path — run it explicitly when you want it.
    run_safe_suite
    ;;
  *)
    echo "Usage: $0 <scenario> [namespace] [auto-rollback]" >&2
    echo >&2
    echo "Scenarios:" >&2
    echo "  db-outage                simulated DB failure (chaos API)" >&2
    echo "  http-errors              forced HTTP 5xx (chaos API)" >&2
    echo "  latency                  injected request latency (chaos API)" >&2
    echo "  notification-degradation notification delivery failures (chaos API)" >&2
    echo "  high-cpu                 CPU exhaustion via the burn worker (chaos API)" >&2
    echo "  memory-leak              retained-memory leak (chaos API)" >&2
    echo "  crashloop                container exits at startup (kubectl, reversible)" >&2
    echo "  bad-deployment           bad release needing a rollback — LEAVES THE" >&2
    echo "                           SYSTEM BROKEN unless the third argument is" >&2
    echo "                           'true' (or BAD_DEPLOYMENT_AUTO_ROLLBACK=true)" >&2
    echo "  full-outage              deployment scaled to 0 replicas (kubectl)" >&2
    echo "  all                      every scenario except bad-deployment" >&2
    echo "  reset-all                clear active chaos and recover demo mutations" >&2
    exit 1
    ;;
esac
