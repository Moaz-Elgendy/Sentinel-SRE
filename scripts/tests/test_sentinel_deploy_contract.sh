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

echo "=== verify_synced_to: the actual production regression (git archive/tar bypasses the index) ==="
# This is the exact failure that shipped: `git diff --quiet <sha> -- <path>`
# is index-backed, and `git archive <sha> -- <path> | tar -x` (what
# sync-scripts actually runs) writes files straight to disk without ever
# touching the index. A clone whose index predates a commit that adds a new
# file under scripts/ reproduces it: after the archive extraction the file
# is correct byte-for-byte on disk, but had `verify_synced_to` still been
# `git diff --quiet`, it would have reported it deleted. verify_synced_to
# must pass here — it reads the target tree and the real filesystem
# directly and is not allowed to consult the index at all.
idx_upstream="$(mktemp -d)"
(
  cd "${idx_upstream}"
  git init --quiet -b main
  git config user.email test@example.com
  git config user.name "Contract Test"
  mkdir -p scripts
  echo "existing" > scripts/existing.sh
  git add -A
  git commit --quiet -m "commit A"
)
IDX_SHA_A="$(git -C "${idx_upstream}" rev-parse HEAD)"
(
  cd "${idx_upstream}"
  echo "brand new" > scripts/brand-new.sh
  git add -A
  git commit --quiet -m "commit B: add a new script"
)
IDX_SHA_B="$(git -C "${idx_upstream}" rev-parse HEAD)"

# A real `git clone`, then checked out at A: HEAD, index and working tree
# are all genuinely, fully consistent at A — exactly like a node's very
# first `git clone`, well before commit B (and brand-new.sh) ever existed.
idx_repo="$(mktemp -d)"
git clone --quiet "${idx_upstream}" "${idx_repo}" >/dev/null
(cd "${idx_repo}" && git checkout --quiet "${IDX_SHA_A}")

REPO_DIR="${idx_repo}"
# The real sync-scripts sequence: wipe scripts/, then extract the target
# commit's tree directly onto disk. This never stages anything, so the
# index is still exactly as it was at commit A — no entry at all for
# brand-new.sh.
(cd "${idx_repo}" && rm -rf scripts && git archive "${IDX_SHA_B}" -- scripts | tar -x)
expect_success "sync-scripts' actual mechanism (archive|tar, no index update) verifies clean" \
  verify_synced_to --exact "${IDX_SHA_B}" scripts
if [ -f "${idx_repo}/scripts/brand-new.sh" ] && [ "$(cat "${idx_repo}/scripts/brand-new.sh")" = "brand new" ]; then
  ok "the new file really is present and correct on disk (not a false pass)"
else
  bad "the new file is missing or wrong on disk — verify_synced_to passed incorrectly"
fi

echo "=== verify_synced_to --exact (scripts/-only: no untracked subtree allowed) ==="
exact_repo="$(mktemp -d)"
(
  cd "${exact_repo}"
  git init --quiet -b main
  git config user.email test@example.com
  git config user.name "Contract Test"
  mkdir -p scripts scripts/tests
  echo "a" > scripts/a.sh
  chmod +x scripts/a.sh
  echo "t" > scripts/tests/t.sh
  git add -A
  git commit --quiet -m "exact-mode fixture"
)
EXACT_SHA="$(git -C "${exact_repo}" rev-parse HEAD)"
REPO_DIR="${exact_repo}"

expect_success "--exact passes on a clean, exactly-synced scripts/" \
  verify_synced_to --exact "${EXACT_SHA}" scripts

echo "leftover" > "${exact_repo}/scripts/stale.sh"
expect_failure "--exact catches an extra/stale file that plain mode ignores" \
  verify_synced_to --exact "${EXACT_SHA}" scripts
expect_success "plain mode still ignores that same extra file" \
  verify_synced_to "${EXACT_SHA}" scripts
rm "${exact_repo}/scripts/stale.sh"

chmod -x "${exact_repo}/scripts/a.sh"
expect_failure "--exact catches a wrong executable bit" \
  verify_synced_to --exact "${EXACT_SHA}" scripts
chmod +x "${exact_repo}/scripts/a.sh"

echo "changed" > "${exact_repo}/scripts/tests/t.sh"
expect_failure "--exact catches a modified file in a nested path" \
  verify_synced_to --exact "${EXACT_SHA}" scripts
echo "t" > "${exact_repo}/scripts/tests/t.sh"

rm "${exact_repo}/scripts/a.sh"
expect_failure "--exact catches a missing file" \
  verify_synced_to --exact "${EXACT_SHA}" scripts
echo "a" > "${exact_repo}/scripts/a.sh"
chmod +x "${exact_repo}/scripts/a.sh"

expect_success "--exact passes again once restored exactly" \
  verify_synced_to --exact "${EXACT_SHA}" scripts

echo "=== ensure_repo_synced (git checkout repair) ==="
# "Upstream" origin: a real repo any of these fixtures can clone/fetch from,
# standing in for GitHub in the tests below. Two commits so there is
# something to be "missing before fetch, available after".
upstream="$(mktemp -d)"
(
  cd "${upstream}"
  git init --quiet -b master
  git config user.email test@example.com
  git config user.name "Contract Test"
  mkdir -p scripts k8s/overlays/aws/secrets
  echo "old" > scripts/thing.sh
  echo "old" > k8s/thing.yaml
  git add -A
  git commit --quiet -m "old commit"
)
UP_OLD_SHA="$(git -C "${upstream}" rev-parse HEAD)"
# Snapshot a clone taken *at this point* — i.e. one that genuinely does not
# have the commit created next below in its local object database. This is
# what "valid-but-missing-commit" fixtures are copied from, since cloning
# from upstream *after* the new commit exists would defeat the point (the
# clone would already have it).
old_clone_template="$(mktemp -d)"
git clone --quiet "${upstream}" "${old_clone_template}"
(
  cd "${upstream}"
  echo "new" > scripts/thing.sh
  echo "new" > k8s/thing.yaml
  git add -A
  git commit --quiet -m "new commit"
)
UP_NEW_SHA="$(git -C "${upstream}" rev-parse HEAD)"

# fixture_repo_dir <mode>: sets REPO_DIR to a fresh tmp dir prepared per
# `mode` and returns it via the global FIXTURE var. Each mode reproduces one
# way a real node's /opt/sentinel-sre has been observed or could plausibly
# end up broken.
FIXTURE=""
fixture_repo_dir() {
  local mode="$1"
  local dir
  dir="$(mktemp -d)"
  FIXTURE="${dir}"
  case "${mode}" in
    valid-and-current)
      git clone --quiet "${upstream}" "${dir}"
      ;;
    valid-but-missing-commit)
      # A checkout genuinely cloned before the upstream's newest commit
      # existed — models an ordinary "CI pushed a new commit, node hasn't
      # fetched yet" gap, not corruption. rmdir first: cp -a into a
      # mktemp -d target requires the target not already contain a
      # conflicting tree.
      rmdir "${dir}"
      cp -a "${old_clone_template}" "${dir}"
      ;;
    missing-head)
      # ".git exists, but nothing has ever been committed to it" —
      # reproduces the exact symptom from the field: `git rev-parse HEAD`
      # is ambiguous/unresolvable, yet the directory (and .git) exist.
      mkdir -p "${dir}"
      ( cd "${dir}" && git init --quiet )
      ;;
    missing-git)
      # Directory exists (application/working-tree files present, as they
      # would be on a real node) but .git never got created at all.
      mkdir -p "${dir}/scripts" "${dir}/k8s"
      ;;
    corrupted-git)
      # A .git directory that exists but is not a valid git repository at
      # all (truncated/garbage HEAD, no object database) — distinct from
      # "missing-head", which is a syntactically valid empty repo.
      mkdir -p "${dir}/.git"
      echo "not a real ref" > "${dir}/.git/HEAD"
      ;;
  esac
  printf '%s' "${dir}"
}

# --- valid, current checkout: no repair, no network needed ---
fixture_repo_dir valid-and-current >/dev/null
REPO_DIR="${FIXTURE}"
REPO_URL_STATE_FILE="/tmp/does-not-exist-sentinel-repo-url-$$"
expect_success "already-valid checkout at the requested commit: no-op" \
  ensure_repo_synced "${UP_NEW_SHA}"

# --- every broken-checkout shape gets repaired, given a resolvable origin ---
repo_url_file="$(mktemp)"
printf '%s' "${upstream}" > "${repo_url_file}"
REPO_URL_STATE_FILE="${repo_url_file}"

for mode in valid-but-missing-commit missing-head missing-git corrupted-git; do
  fixture_repo_dir "${mode}" >/dev/null
  REPO_DIR="${FIXTURE}"
  expect_success "repairs '${mode}' and reaches the requested commit" \
    ensure_repo_synced "${UP_NEW_SHA}"
  if git -C "${REPO_DIR}" cat-file -e "${UP_NEW_SHA}^{commit}" 2>/dev/null; then
    ok "'${mode}': commit is actually present in the local object db afterward"
  else
    bad "'${mode}': commit still not present in the local object db afterward"
  fi
done

# --- fails closed: no origin can be discovered, nothing to repair from ---
fixture_repo_dir missing-git >/dev/null
REPO_DIR="${FIXTURE}"
REPO_URL_STATE_FILE="/tmp/does-not-exist-sentinel-repo-url-$$"
expect_failure "fails closed when no repo URL is known and .git is unusable" \
  ensure_repo_synced "${UP_NEW_SHA}"

# --- fails closed: origin resolvable, but the requested commit genuinely
#     does not exist there (never fetchable, no matter how many retries) ---
fixture_repo_dir missing-git >/dev/null
REPO_DIR="${FIXTURE}"
REPO_URL_STATE_FILE="${repo_url_file}"
expect_failure "fails closed when the requested commit does not exist upstream" \
  ensure_repo_synced "0000000000000000000000000000000000dead"

# --- preserves untracked content, most importantly the live AWS secrets,
#     across a full repair (missing .git entirely) ---
fixture_repo_dir missing-git >/dev/null
REPO_DIR="${FIXTURE}"
REPO_URL_STATE_FILE="${repo_url_file}"
mkdir -p "${REPO_DIR}/k8s/overlays/aws/secrets"
echo "super-secret-value" > "${REPO_DIR}/k8s/overlays/aws/secrets/citizen-postgres.env"
before_secret="$(cat "${REPO_DIR}/k8s/overlays/aws/secrets/citizen-postgres.env")"
expect_success "repairs a missing .git with live secrets already on disk" \
  ensure_repo_synced "${UP_NEW_SHA}"
after_secret="$(cat "${REPO_DIR}/k8s/overlays/aws/secrets/citizen-postgres.env" 2>/dev/null || echo "<gone>")"
if [ "${before_secret}" = "${after_secret}" ]; then
  ok "untracked k8s/overlays/aws/secrets/*.env survives a .git repair byte-for-byte"
else
  bad "untracked secret was altered or deleted by a .git repair (before='${before_secret}' after='${after_secret}')"
fi

echo "=== end-to-end: sync-scripts / sync-manifests / sync recover from a broken checkout ==="
# These call main() itself (not just the helpers), the same entry point CI
# and SSM use, against a REPO_DIR whose .git is unusable — reproducing the
# exact field symptom (git rev-parse HEAD fails) and confirming the
# documented contract still holds end-to-end: sync-scripts fully replaces
# scripts/ (incl. deletions) and reinstalls the bin copy with correct
# executable bits; sync-manifests touches only k8s/ and never the live
# secrets; both fail closed if verification doesn't pass.

# sync-scripts: upstream's "new commit" removed nothing, so exercise a real
# deletion too, matching the original field failure (files disappearing).
(
  cd "${upstream}"
  echo "old" > scripts/removed-in-latest.sh
  git add -A
  git commit --quiet -m "add a file that gets removed next"
)
(
  cd "${upstream}"
  git rm --quiet scripts/removed-in-latest.sh
  git commit --quiet -m "remove it again"
)
UP_LATEST_SHA="$(git -C "${upstream}" rev-parse HEAD)"

fixture_repo_dir missing-head >/dev/null
REPO_DIR="${FIXTURE}"
REPO_URL_STATE_FILE="${repo_url_file}"
INSTALL_BIN_DIR="$(mktemp -d)"
# sync-scripts installs to /usr/local/bin/sentinel-deploy.sh unconditionally;
# these tests run unprivileged, so only assert on what sync-scripts leaves
# inside REPO_DIR itself, which is what the field failure was actually about.
if MODE=sync-scripts SHA="${UP_LATEST_SHA}" \
  bash -c 'set -euo pipefail; cd '"${SCRIPT_DIR}"'/.. ; source ./sentinel-deploy.sh; REPO_DIR="'"${REPO_DIR}"'"; REPO_URL_STATE_FILE="'"${REPO_URL_STATE_FILE}"'"; main sync-scripts "'"${UP_LATEST_SHA}"'" 2>&1 | tee /tmp/sync-scripts-test.out; exit "${PIPESTATUS[0]}"' \
  >/tmp/sync-scripts-test.out 2>&1; then
  ok "sync-scripts recovers from a checkout with no resolvable HEAD"
else
  bad "sync-scripts still fails against a broken checkout (output: $(cat /tmp/sync-scripts-test.out))"
fi
if [ ! -e "${REPO_DIR}/scripts/removed-in-latest.sh" ]; then
  ok "sync-scripts: a file deleted upstream is actually gone after recovery"
else
  bad "sync-scripts: a file deleted upstream is still present after recovery"
fi
if [ -x "${REPO_DIR}/scripts/thing.sh" ]; then
  ok "sync-scripts: executable bit is set on synced scripts after recovery"
else
  bad "sync-scripts: executable bit missing on synced scripts after recovery"
fi

# sync-manifests: same broken-checkout starting point, but this time with a
# live untracked secret sitting in k8s/ that must survive.
fixture_repo_dir missing-git >/dev/null
REPO_DIR="${FIXTURE}"
REPO_URL_STATE_FILE="${repo_url_file}"
mkdir -p "${REPO_DIR}/k8s/overlays/aws/secrets"
echo "super-secret-value" > "${REPO_DIR}/k8s/overlays/aws/secrets/citizen-postgres.env"
if bash -c 'set -euo pipefail; cd '"${SCRIPT_DIR}"'/.. ; source ./sentinel-deploy.sh; REPO_DIR="'"${REPO_DIR}"'"; REPO_URL_STATE_FILE="'"${REPO_URL_STATE_FILE}"'"; main sync-manifests "'"${UP_LATEST_SHA}"'"' \
  >/tmp/sync-manifests-test.out 2>&1; then
  ok "sync-manifests recovers from a missing .git and syncs k8s/"
else
  bad "sync-manifests still fails against a missing .git (output: $(cat /tmp/sync-manifests-test.out))"
fi
if [ "$(cat "${REPO_DIR}/k8s/overlays/aws/secrets/citizen-postgres.env" 2>/dev/null)" = "super-secret-value" ]; then
  ok "sync-manifests: live untracked secret survives a full checkout recovery"
else
  bad "sync-manifests: live untracked secret was lost during checkout recovery"
fi
if [ "$(cat "${REPO_DIR}/k8s/thing.yaml" 2>/dev/null)" = "new" ]; then
  ok "sync-manifests: k8s/ content matches the requested commit after recovery"
else
  bad "sync-manifests: k8s/ content does not match the requested commit after recovery"
fi

# --- true end-to-end reproduction of the production failure, through the
#     actual `main sync-scripts` entry point, on a HEALTHY (never broken)
#     clone — not the missing-HEAD repair fixture above, which populates
#     the index as a side effect of repair and would not have caught this.
#     A real `git clone` checked out at an older commit, exactly like a
#     node that was provisioned before a later push added a new script and
#     has not been told to sync-scripts since.
e2e_upstream="$(mktemp -d)"
(
  cd "${e2e_upstream}"
  git init --quiet -b master
  git config user.email test@example.com
  git config user.name "Contract Test"
  mkdir -p scripts
  echo "old" > scripts/existing.sh
  git add -A
  git commit --quiet -m "old commit"
)
E2E_OLD_SHA="$(git -C "${e2e_upstream}" rev-parse HEAD)"
(
  cd "${e2e_upstream}"
  echo "new script" > scripts/newly-added.sh
  git add -A
  git commit --quiet -m "add a new script"
)
E2E_NEW_SHA="$(git -C "${e2e_upstream}" rev-parse HEAD)"

e2e_repo="$(mktemp -d)"
git clone --quiet "${e2e_upstream}" "${e2e_repo}" >/dev/null
(cd "${e2e_repo}" && git checkout --quiet "${E2E_OLD_SHA}")
REPO_DIR="${e2e_repo}"
REPO_URL_STATE_FILE="${repo_url_file}"

if MODE=sync-scripts SHA="${E2E_NEW_SHA}" \
  bash -c 'set -euo pipefail; cd '"${SCRIPT_DIR}"'/.. ; source ./sentinel-deploy.sh; REPO_DIR="'"${REPO_DIR}"'"; REPO_URL_STATE_FILE="'"${REPO_URL_STATE_FILE}"'"; main sync-scripts "'"${E2E_NEW_SHA}"'"' \
  >/tmp/sync-scripts-e2e-test.out 2>&1; then
  ok "sync-scripts on a healthy clone whose index predates a new file: verifies clean (the actual production bug)"
else
  bad "sync-scripts still fails on a healthy clone with a stale index (output: $(cat /tmp/sync-scripts-e2e-test.out))"
fi
if [ "$(cat "${e2e_repo}/scripts/newly-added.sh" 2>/dev/null)" = "new script" ]; then
  ok "sync-scripts: the newly-added file is actually present and correct on disk"
else
  bad "sync-scripts: the newly-added file is missing or wrong on disk"
fi

echo
echo "============================================================"
echo " ${PASS} passed, ${FAIL} failed"
echo "============================================================"
[ "${FAIL}" -eq 0 ]
