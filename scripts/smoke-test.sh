#!/bin/bash
# Phase 8 smoke test — exercises the full flow described in Phases.md
# ("Try it out") against a live deployment, through the Ingress, the same
# way a real browser would: register -> login -> browse services -> submit
# a request -> confirm it shows up -> confirm notification-service saw it.
#
# Usage: ./scripts/smoke-test.sh [base_url]
#   base_url defaults to http://citizen-portal.local (the Ingress host).
#   Pass http://localhost:8000 to test citizen-service directly without
#   the Ingress in front of it.
set -euo pipefail

BASE_URL="${1:-http://citizen-portal.local}"
EMAIL="smoketest-$(date +%s)@example.com"
PASSWORD="SmokeTest123!"

json_get() {
  # $1 = json string, $2 = key -> minimal dependency-free extraction
  python3 -c "import sys, json; print(json.loads(sys.argv[1]).get(sys.argv[2], ''))" "$1" "$2" 2>/dev/null || echo ""
}

echo "==> Target: $BASE_URL"

echo "==> [1/6] Registering a test citizen ($EMAIL)"
REGISTER_RESPONSE=$(curl -sf -X POST "$BASE_URL/api/auth/register" \
  -H "Content-Type: application/json" \
  -d "{\"full_name\":\"Smoke Test\",\"national_id\":\"$(date +%s)00000\",\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}")
echo "    OK"

echo "==> [2/6] Logging in"
LOGIN_RESPONSE=$(curl -sf -X POST "$BASE_URL/api/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}")
TOKEN=$(json_get "$LOGIN_RESPONSE" "access_token")
if [ -z "$TOKEN" ]; then
  echo "FAILED: no access_token in login response: $LOGIN_RESPONSE" >&2
  exit 1
fi
echo "    OK — got a token"

echo "==> [3/6] Browsing government services"
SERVICES_RESPONSE=$(curl -sf "$BASE_URL/api/services")
SERVICE_ID=$(python3 -c "import sys, json; d = json.loads(sys.argv[1]); print(d[0]['id'] if d else '')" "$SERVICES_RESPONSE" 2>/dev/null || echo "")
if [ -z "$SERVICE_ID" ]; then
  echo "FAILED: no services returned — was the seed step (initContainer) successful?" >&2
  echo "Response: $SERVICES_RESPONSE" >&2
  exit 1
fi
echo "    OK — found service $SERVICE_ID"

echo "==> [4/6] Submitting a request"
REQUEST_RESPONSE=$(curl -sf -X POST "$BASE_URL/api/requests" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d "{\"service_id\":\"$SERVICE_ID\"}")
REQUEST_ID=$(json_get "$REQUEST_RESPONSE" "id")
if [ -z "$REQUEST_ID" ]; then
  echo "FAILED: no request id in response: $REQUEST_RESPONSE" >&2
  exit 1
fi
echo "    OK — created request $REQUEST_ID"

echo "==> [5/6] Confirming it shows up in 'My Requests'"
MY_REQUESTS=$(curl -sf "$BASE_URL/api/requests" -H "Authorization: Bearer $TOKEN")
if ! echo "$MY_REQUESTS" | grep -q "$REQUEST_ID"; then
  echo "FAILED: request $REQUEST_ID not found in /api/requests: $MY_REQUESTS" >&2
  exit 1
fi
echo "    OK"

echo "==> [6/6] Checking notification-service dispatch (best-effort, may take a moment)"
sleep 2
# notification-service is intentionally not exposed through the Ingress
# (server-to-server only — see Phases.md Phase 7). Reach it directly via
# kubectl port-forward in another terminal if you want to check this from
# outside the cluster; inside a cluster debug pod, this would be:
#   curl http://notification-service.citizen-portal.svc.cluster.local:8000/api/notifications/<citizen_id>
echo "    Skipped automatic check — notification-service has no Ingress rule by design."
echo "    Verify manually with:"
echo "      kubectl port-forward -n citizen-portal svc/notification-service 8001:8000"
echo "      curl http://localhost:8001/api/notifications/<citizen_id_from_/api/auth/me>"

echo
echo "============================================================"
echo " Smoke test passed."
echo "============================================================"
