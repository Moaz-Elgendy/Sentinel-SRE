#!/usr/bin/env bash
# Offline contract tests for scripts/incident-scenarios.sh.
# These protect the end-to-end demo contract: a chaos fault must stay active
# until Alertmanager has received the firing alert and Sentinel has had a
# chance to clear the corresponding chaos state.
set -euo pipefail

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/incident-scenarios.sh"

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "ok - $1"; }

[[ -f "$SCRIPT" ]] || fail "scenario runner missing"

bash -n "$SCRIPT"
pass "scenario runner has valid bash syntax"

grep -q '^wait_for_alertmanager() {' "$SCRIPT" \
  || fail "wait_for_alertmanager helper missing"
pass "Alertmanager propagation gate exists"

grep -q '^wait_for_chaos_clear() {' "$SCRIPT" \
  || fail "wait_for_chaos_clear helper missing"
pass "Sentinel remediation wait helper exists"

grep -q 'SENTINEL_REMEDIATION_TIMEOUT="\${SENTINEL_REMEDIATION_TIMEOUT:-300}"' "$SCRIPT" \
  || fail "default remediation timeout is missing"
pass "five-minute Sentinel remediation window is configurable"

extract_function() {
  local name="$1"
  awk -v target="^""$name""[[:space:]]*\\(\\)[[:space:]]*\\{$" \
    'BEGIN{inside=0} $0 ~ target {inside=1} inside {print} inside && /^}$/ {exit}' "$SCRIPT"
}

for scenario in db_outage http_errors latency notification_degradation high_cpu memory_leak; do
  body="$(extract_function "scenario_${scenario}")"
  [[ -n "$body" ]] || fail "could not extract scenario_${scenario}"
  printf '%s\n' "$body" | grep -q 'wait_for_alertmanager ' \
    || fail "scenario_${scenario} does not wait for Alertmanager"
  printf '%s\n' "$body" | grep -q 'wait_for_chaos_clear ' \
    || fail "scenario_${scenario} does not wait for Sentinel remediation"
  pass "scenario_${scenario} waits for Alertmanager and Sentinel"
done

echo "All incident-scenario contract tests passed."
