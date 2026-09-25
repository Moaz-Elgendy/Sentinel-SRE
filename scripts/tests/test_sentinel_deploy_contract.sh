#!/usr/bin/env bash
# Offline tests for the deployment-synchronization contract in
# scripts/sentinel-deploy.sh: sync must not be best-effort, a survived
# placeholder must stop the deploy, a missing executable bit must be
# caught, and "sync succeeded" must actually mean "the working tree matches
# the requested commit". None of this needs AWS credentials, a kubeconfig,
# or a live cluster — that's the point: these are the failure modes that
# were previously only checkable by SSMing into the real node.
#
# sentinel-deploy.sh is written so `source`-ing it (rather than executing
# it) only defines its functions and does nothing else — see the
# `if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then main "$@"; fi` guard at
# the bottom of that file. That is what makes this possible.
#
# Usage: ./scripts/tests/test_sentinel_deploy_contract.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/../sentinel-deploy.sh"

PASS=0
FAIL=0

ok() {
  PASS=$((PASS + 1))
  echo "  ok   - $1"
}

bad() {
  FAIL=$((FAIL + 1))
  echo "  FAIL - $1"
}

# expect_success <description> <command...>
expect_success() {
  local desc="$1"
  shift
  if "$@" >/tmp/sentinel-deploy-test.out 2>&1; then
    ok "$desc"
  else
    bad "$desc (expected success, got exit $?; output: $(cat /tmp/sentinel-deploy-test.out))"
  fi
}

# expect_failure <description> <command...>
expect_failure() {
  local desc="$1"
  shift
  if "$@" >/tmp/sentinel-deploy-test.out 2>&1; then
    bad "$desc (expected failure, but it succeeded)"
  else
    ok "$desc"
  fi
}

echo "=== validate_upstream_url ==="
expect_success "accepts a private-IP http URL"      validate_upstream_url "http://10.20.1.57:8080"
expect_success "accepts the in-cluster DNS default" validate_upstream_url "http://sentinel-ai:8080"
expect_success "accepts https"                      validate_upstream_url "https://sentinel.example.com"
expect_failure "rejects the raw placeholder"        validate_upstream_url "SENTINEL_API_UPSTREAM_PLACEHOLDER"
expect_failure "rejects empty"                      validate_upstream_url ""
expect_failure "rejects a non-http(s) scheme"       validate_upstream_url "ftp://10.20.1.57"
expect_failure "rejects a bare host with no scheme" validate_upstream_url "10.20.1.57:8080"

echo "=== assert_no_placeholders ==="
clean_file="$(mktemp)"
dirty_file="$(mktemp)"
trap 'rm -f "${clean_file}" "${dirty_file}"' EXIT
cat >"${clean_file}" <<'EOF'
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      containers:
        - env:
            - name: SENTINEL_API_UPSTREAM
              value: http://10.20.1.57:8080
EOF
cat >"${dirty_file}" <<'EOF'
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      containers:
        - env:
            - name: SENTINEL_API_UPSTREAM
              value: SENTINEL_API_UPSTREAM_PLACEHOLDER
EOF
expect_success "passes a fully-substituted manifest" assert_no_placeholders "${clean_file}"
expect_failure "catches a manifest with a surviving placeholder" assert_no_placeholders "${dirty_file}"

echo "=== verify_executable ==="
exec_file="$(mktemp)"
chmod +x "${exec_file}"
noexec_file="$(mktemp)"
chmod -x "${noexec_file}"
trap 'rm -f "${exec_file}" "${noexec_file}"' EXIT
expect_success "passes a chmod +x file"    verify_executable "${exec_file}"
expect_failure "catches a non-executable file" verify_executable "${noexec_file}"
expect_failure "catches a missing file"        verify_executable "/tmp/does-not-exist-sentinel-deploy-test"

echo "=== verify_synced_to (this is what replaces 'trust the exit code') ==="
tmp_repo="$(mktemp -d)"
trap 'rm -rf "${tmp_repo}"' EXIT
(
  cd "${tmp_repo}"
  git init --quiet -b main
  git config user.email test@example.com
  git config user.name "Contract Test"
  mkdir -p scripts k8s
  echo "old" > scripts/thing.sh
  echo "old" > k8s/thing.yaml
  git add -A
  git commit --quiet -m "old commit"
)
OLD_SHA="$(git -C "${tmp_repo}" rev-parse HEAD)"
(
  cd "${tmp_repo}"
  echo "new" > scripts/thing.sh
  echo "new" > k8s/thing.yaml
  git add -A
  git commit --quiet -m "new commit"
)
NEW_SHA="$(git -C "${tmp_repo}" rev-parse HEAD)"

# Simulate what sync-scripts/sync-manifests do: point REPO_DIR at our fixture
# repo (normally /opt/sentinel-sre) and exercise the same verify_synced_to
# calls they make.
REPO_DIR="${tmp_repo}"

expect_success "working tree already matches HEAD (new)" verify_synced_to "${NEW_SHA}" scripts k8s

# Roll the working tree back to the old content WITHOUT moving HEAD, the
# same shape of drift that motivated this check: something that looks
# synced (git log says NEW_SHA) but whose files on disk are stale.
(cd "${tmp_repo}" && git checkout --quiet "${OLD_SHA}" -- scripts k8s)
expect_failure "catches a working tree that still has stale content" verify_synced_to "${NEW_SHA}" scripts k8s

# Now actually sync it forward, the way sync-manifests/sync-scripts do.
(cd "${tmp_repo}" && git checkout --quiet "${NEW_SHA}" -- scripts k8s)
expect_success "passes once the working tree is brought forward" verify_synced_to "${NEW_SHA}" scripts k8s

# An untracked file sitting alongside tracked ones (standing in for
# k8s/overlays/aws/secrets/*.env, which is real and gitignored in the
# actual repo) must never trip this check.
echo "totally-untracked-secret" > "${tmp_repo}/k8s/secret.env"
expect_success "ignores an untracked file (models secrets/*.env)" verify_synced_to "${NEW_SHA}" scripts k8s

echo
echo "============================================================"
echo " ${PASS} passed, ${FAIL} failed"
echo "============================================================"
[ "${FAIL}" -eq 0 ]
