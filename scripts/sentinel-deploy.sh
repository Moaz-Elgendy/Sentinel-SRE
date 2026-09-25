#!/usr/bin/env bash
# Deploy a specific image tag to the citizen portal namespace on the K3s
# node, or refresh this script itself on the node.
#
# Usage:
#   sentinel-deploy.sh images         <git-sha>  # roll the app/Sentinel images to <git-sha>
#   sentinel-deploy.sh apply          <git-sha>  # full git checkout <git-sha> + kubectl apply -k
#   sentinel-deploy.sh sync-manifests <git-sha>  # refresh ONLY k8s/**/*.yaml from <git-sha>
#   sentinel-deploy.sh apply-manifests <git-sha> # render+substitute+apply whatever is on disk now
#   sentinel-deploy.sh sync-scripts   <git-sha>  # replace scripts/ (incl. deletions) from <git-sha>
#   sentinel-deploy.sh sync  <git-sha>    # refresh THIS script on the node from <git-sha> (narrow;
#                                          # superseded for CI's purposes by sync-scripts above,
#                                          # kept for any existing manual/documented use)
#
# "images" is the normal CI path when a push touches no k8s/ files: it
# changes only the container images, which produces a clean new ReplicaSet
# and therefore real rollout history for Sentinel to correlate against and
# roll back to.
#
# "apply" is the original manifest-change path: a full `git checkout
# --detach` of the WHOLE repository at ${REPO_DIR}, then render+apply. Left
# in place for manual/operator use over SSM (see docs/aws-deployment.md) —
# it still works exactly as before.
#
# "sync-manifests" + "apply-manifests" are the CI-automated manifest-change
# path (see .github/workflows/ci-cd.yml's deploy-to-k3s job), split into two
# steps deliberately narrower than "apply":
#   * sync-manifests does `git checkout <sha> -- k8s` — this touches ONLY
#     tracked files under k8s/, never anything else in the repo (no
#     citizen-service/, no infra/terraform/, etc). `git checkout <path>`
#     only ever writes paths that are tracked by git, so this can never
#     touch k8s/overlays/aws/secrets/*.env — those are gitignored
#     (untracked) precisely so a manifest sync can never overwrite the real,
#     live secret values on the node. Only the *.env.example templates are
#     tracked, and syncing them is harmless.
#     Known limitation, accepted deliberately: `git checkout -- <path>` only
#     adds/updates files, it cannot remove one that no longer exists at
#     <sha> — an orphaned manifest can linger on disk. That is judged safe
#     because Kustomize only ever reads files a kustomization.yaml actually
#     lists as a resource; an unreferenced leftover file is inert. The
#     alternative (wipe k8s/ and re-extract) is NOT used here, because it
#     would also delete the live secrets/*.env sitting inside
#     k8s/overlays/aws/secrets/ — exactly what must never happen.
#   * apply-manifests does the render+substitute+apply from whatever is
#     already on disk at ${REPO_DIR}/k8s — it does NOT check anything out
#     itself, so it must be run right after sync-manifests.
#
# "sync-scripts" keeps scripts/ current on the node. Two things read it live
# at runtime, not just at deploy time:
#   * The external Sentinel EC2's chaos-scenario runner SSMs into THIS node
#     and runs `cd /opt/sentinel-sre && ./scripts/incident-scenarios.sh ...`
#     on demand, whenever an operator triggers a scenario from the Sentinel
#     GUI (see sentinel-ai/app/routers/chaos_scenarios.py). A stale copy
#     here silently runs old scenario logic.
#   * scripts/deploy-aws.sh and scripts/generate-aws-secrets.sh are the
#     documented manual-operator entry points (docs/aws-deployment.md),
#     invoked directly from this checkout over an SSM session.
# Unlike k8s/, scripts/ has no untracked subtree that must be protected, so
# it is safe to fully replace: `git archive <sha> -- scripts | tar -x`
# extracts exactly what is in that commit — including removing a file that
# was deleted there, which `git checkout -- <path>` cannot do. This also
# reinstalls /usr/local/bin/sentinel-deploy.sh, so it supersedes the
# original narrow "sync" mode below for CI's purposes.
#
# "sync" exists because this script is installed on the node once, at first
# boot, by Terraform (see infra/terraform/user_data.sh.tftpl, which embeds
# THIS file verbatim). Terraform intentionally will NOT push a changed
# user_data to an already-running instance (user_data_replace_on_change =
# false in infra/terraform/ec2.tf — changing user_data must not silently
# replace, and wipe, a live cluster). That means a fix made here, in the
# repository, has no effect on a node that was already provisioned until
# something explicitly re-installs this file on it. "sync" is that
# something: CI calls it, over the same narrow SSM path as "images", before
# every "images" deploy, so a script fix (e.g. adding a new service to the
# "images" case below) reaches the live node on the very next push to main
# — no manual server access needed.
#
# ---------------------------------------------------------------------------
# Where this file is installed from
# ---------------------------------------------------------------------------
# * First boot: infra/terraform/ec2.tf reads this file with Terraform's
#   file() and splices it verbatim into user_data.sh.tftpl, which writes it
#   to /usr/local/bin/sentinel-deploy.sh. There is exactly one copy of the
#   deploy logic — this file — so it cannot drift from what actually runs.
# * Every CI deploy: `sync` (see the case below) re-copies this exact file
#   from the node's own checkout at ${REPO_DIR} (kept current by `git fetch`
#   inside `sync` itself) over /usr/local/bin/sentinel-deploy.sh.
#
# Because of this, avoid anything here that only makes sense inside a
# Terraform-rendered heredoc (no ${{...}} double-dollar escaping needed —
# this is a plain, ordinary bash script).
set -euo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# Fixed for this environment — mirrors infra/terraform/variables.tf's
# app_namespace/project_name defaults and ec2.tf's repo_dir. These three
# things do not vary independently of a full infrastructure redeploy, so
# they are constants here rather than plumbed through as arguments; that
# keeps the CI call site (`sentinel-deploy.sh images <sha>`) unchanged.
NS="citizen-portal"
PREFIX="sentinel-sre-demo"
REPO_DIR="/opt/sentinel-sre"

# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------
# These exist so "the SSM command returned 0" is never mistaken for "the
# node is now at the commit CI asked for" — see docs/aws-deployment.md,
# "Deployment synchronization contract", for the incident that made that
# distinction necessary: sync-scripts used to be allowed to fail silently,
# which let a manifest change ship with an old copy of render_and_apply()
# that didn't know about it, and nothing here caught that until the bad
# value was already live.

# Fails loudly if `path` is not executable. Kept separate from `chmod`
# itself so every caller proves the bit actually landed, rather than
# trusting that `install`/`chmod`'s own exit code was enough — on a
# filesystem mounted noexec, or if `install`/`chmod` silently no-ops on a
# path it doesn't have permission to touch, the command can still return 0.
verify_executable() {
  local path="$1"
  if [ ! -x "${path}" ]; then
    echo "ERROR: ${path} is not executable after sync." >&2
    stat "${path}" >&2 2>/dev/null || echo "(and it does not exist at all)" >&2
    return 1
  fi
}

# Fails loudly if the filesystem under any of `paths` does not exactly
# match the tree at `sha` — same files, same content, same executable bit.
#
# THIS USED TO BE `git diff --quiet <sha> -- <paths>`. That was the root
# cause of a real production outage: `sync-scripts` populates the working
# tree with `git archive <sha> -- scripts | tar -x`, which writes files
# straight to disk and never touches the Git index. `git diff <commit>`
# compares the working tree against `<commit>` *through* the index as an
# intermediate cache — a path with no index entry at all (or a stale one
# left over from before the archive extraction) reads as "absent" to that
# diff, no matter what is actually sitting on disk. The result was
# `verify_synced_to` reporting every file under scripts/ as deleted right
# after `sync-scripts` had just correctly written every one of them,
# blocking every subsequent deploy. This has been reproduced against this
# exact repository: a clone whose index predates a commit that adds a new
# script, followed by the real `rm -rf scripts; git archive "$SHA" --
# scripts | tar -x` sync-scripts does, makes the old `git diff --quiet`
# check fail even though `diff -r` against the target tree finds nothing
# wrong. `git diff`/`git status`-based checks are index-backed by
# construction, so no flag or extra call fixes this — the check has to stop
# consulting the index at all.
#
# This instead reads the *target tree* directly with `git ls-tree -r`
# (mode + blob sha + path for every blob under `path` at `sha`) and
# re-derives the identical triple from the *real filesystem* with `stat`
# (executable bit) and `git hash-object` (content-addressed, without
# staging or writing anything) for every regular file actually present.
# Two plain strings are then compared. This never reads or writes the
# index, so it is correct regardless of which sync mechanism populated the
# working tree (`git archive | tar`, `git checkout -- <path>`, or a manual
# copy) and regardless of what state the index happens to be in. It proves
# the one thing CI actually cares about: what will really execute from
# ${REPO_DIR}, not what some cache believes is there.
#
# Default (no --exact): silent about any file present on disk under `path`
# that the tree at `sha` does not track — the same contract the old
# `git diff`-based check had. This is what makes it safe for k8s/, which
# has live untracked secrets (k8s/overlays/aws/secrets/*.env) that must
# never trip this check, and preserves sync-manifests' documented, accepted
# limitation that `git checkout -- <path>` cannot remove an orphaned
# manifest.
#
# --exact: additionally fails if a file exists on disk under `path` that
# the tree at `sha` does not track. Only safe — and only used — for
# scripts/, which sync-scripts always `rm -rf`s immediately before
# re-extracting from `sha` (see sync-scripts below), so scripts/ has no
# untracked subtree of its own by construction; this is belt-and-suspenders
# verification that the rm -rf + archive step actually did that, not a
# requirement that changes what any caller has to do.
verify_synced_to() {
  local exact=0
  if [ "${1:-}" = "--exact" ]; then
    exact=1
    shift
  fi
  local sha="$1"
  shift
  local path expected actual tracked_paths failed=0

  for path in "$@"; do
    # "<mode> <blob-sha> <path>" for every blob the tree at ${sha} has
    # under ${path}, one per line, sorted by path.
    expected="$(git -C "${REPO_DIR}" ls-tree -r "${sha}" -- "${path}" \
      | awk '{printf "%s %s %s\n", $1, $3, $4}' | LC_ALL=C sort -k3,3)"

    # The identical triple, re-derived from what is really on disk right
    # now — never from the index, never from `git status`/`git diff`.
    actual="$(cd "${REPO_DIR}" && find "${path}" -type f 2>/dev/null | while IFS= read -r f; do
        if [ -x "${f}" ]; then mode=100755; else mode=100644; fi
        blob="$(git hash-object "${f}")"
        printf '%s %s %s\n' "${mode}" "${blob}" "${f}"
      done | LC_ALL=C sort -k3,3)"

    if [ "${exact}" -eq 0 ]; then
      # Drop any on-disk file whose path isn't tracked at ${sha} under
      # this pathspec before comparing (live secrets, or anything else
      # legitimately untracked) — same as the old check's silence on
      # paths Git doesn't track.
      tracked_paths="$(printf '%s\n' "${expected}" | awk 'NF{print $3}')"
      actual="$(printf '%s\n' "${actual}" | awk -v trackedlist="${tracked_paths}" '
        BEGIN {
          n = split(trackedlist, arr, "\n")
          for (i = 1; i <= n; i++) if (arr[i] != "") tracked[arr[i]] = 1
        }
        NF { if ($3 in tracked) print }
      ')"
    fi

    if [ "${expected}" != "${actual}" ]; then
      echo "ERROR: ${REPO_DIR}/${path} is not actually at ${sha}" >&2
      echo "       (sync reported success but the filesystem still differs" >&2
      echo "       from the requested commit's tree — do not deploy from this state)" >&2
      diff <(printf '%s\n' "${expected}") <(printf '%s\n' "${actual}") >&2 || true
      failed=1
    fi
  done

  [ "${failed}" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Repo repair / bootstrap
# ---------------------------------------------------------------------------
# File Terraform's bootstrap (user_data.sh.tftpl) writes the configured
# clone URL into, so that any later repair — on a node bootstrapped before
# this file existed, or one whose repo_url differs from a hardcoded guess —
# still has a real origin to recover from without anyone having to know or
# retype it. Not sensitive: it is the same public HTTPS clone URL already
# visible in `git remote -v` and in Terraform's own state.
REPO_URL_STATE_FILE="${REPO_URL_STATE_FILE:-/etc/sentinel-sre/repo-url}"

# True if REPO_DIR is a git working tree with a resolvable HEAD. This, not
# "does .git exist", is the actual precondition every sync mode needs: a
# `.git` directory can exist and still be unusable (empty, from an
# interrupted clone; corrupted; or a bare `git init` with no history), which
# is exactly the failure this function exists to catch instead of trusting.
repo_is_usable() {
  git -C "${REPO_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    && git -C "${REPO_DIR}" rev-parse --verify -q HEAD >/dev/null 2>&1
}

# True if `sha`'s commit object is present in REPO_DIR's local object
# database right now (no network access).
commit_available() {
  local sha="$1"
  git -C "${REPO_DIR}" cat-file -e "${sha}^{commit}" 2>/dev/null
}

# Best-effort discovery of the origin clone URL, preferring the durable
# state file (survives a repair that has to remove .git entirely) and
# falling back to whatever REPO_DIR's own remote says, for a repo that is
# still git-valid but merely missing the requested commit.
resolve_repo_url() {
  local url=""
  if [ -s "${REPO_URL_STATE_FILE}" ]; then
    url="$(cat "${REPO_URL_STATE_FILE}")"
  fi
  if [ -z "${url}" ] && git -C "${REPO_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    url="$(git -C "${REPO_DIR}" remote get-url origin 2>/dev/null || true)"
  fi
  printf '%s' "${url}"
}

# Ensures REPO_DIR is a usable git checkout with `sha` resolvable locally,
# repairing the checkout in place if it is not — reinitializing Git
# metadata and re-pointing it at origin, then fetching, rather than trusting
# a directory that merely exists. This is what every sync mode below calls
# instead of the old `[ ! -d "${REPO_DIR}/.git" ]` check, which only proved
# a directory named .git was present, not that it contained a usable
# repository — the exact gap that let a broken checkout reach
# verify_synced_to instead of being caught and repaired up front.
#
# Safety invariants, load-bearing for the rest of the deployment contract:
#   * REPO_DIR itself is never removed or recreated — only .git may be.
#   * Every other file already on disk in REPO_DIR, tracked or not
#     (including the live, gitignored k8s/overlays/aws/secrets/*.env
#     files), is left exactly as it was. A repair here never runs anything
#     resembling `rm -rf "${REPO_DIR}"` or a wipe-and-reclone of the whole
#     tree.
#   * Fails closed: if the requested commit still cannot be proven present
#     locally after every repair/fetch attempt, this returns non-zero and
#     the caller must not proceed to deploy.
ensure_repo_synced() {
  local sha="$1"
  mkdir -p "${REPO_DIR}"
  git config --global --add safe.directory "${REPO_DIR}" 2>/dev/null || true

  if ! repo_is_usable; then
    echo "WARN: ${REPO_DIR} has no usable git checkout (.git missing, empty, or corrupted)." >&2
    echo "      repairing Git metadata in place (untracked working-tree files, including" >&2
    echo "      k8s/overlays/aws/secrets/*.env, are left untouched) ..." >&2
    local url
    url="$(resolve_repo_url)"
    if [ -z "${url}" ]; then
      echo "ERROR: cannot repair ${REPO_DIR}: no repository URL is known." >&2
      echo "       Neither ${REPO_URL_STATE_FILE} nor an existing origin remote is available." >&2
      echo "       See docs/aws-deployment.md, 'Deployment synchronization contract'." >&2
      return 1
    fi
    # Only .git is ever removed here — see the safety invariants above.
    rm -rf "${REPO_DIR}/.git"
    git -C "${REPO_DIR}" init --quiet
    git -C "${REPO_DIR}" remote add origin "${url}"
    git config --global --add safe.directory "${REPO_DIR}" 2>/dev/null || true
  fi

  if ! commit_available "${sha}"; then
    echo "${REPO_DIR}: ${sha} not present locally yet, fetching from origin ..." >&2
    git -C "${REPO_DIR}" fetch --quiet --all --tags 2>/dev/null || true
    if ! commit_available "${sha}"; then
      # Covers a sha that isn't the tip of any ref `--all` picked up (e.g.
      # an unmerged PR head). GitHub enables fetch-by-SHA
      # (uploadpack.allowReachableSHA1InWant) for reachable commits on
      # public repos, which is the only origin access this script has ever
      # relied on.
      git -C "${REPO_DIR}" fetch --quiet origin "${sha}" 2>/dev/null || true
    fi
  fi

  if ! commit_available "${sha}"; then
    echo "ERROR: ${sha} is not available from ${REPO_DIR}'s origin after fetching." >&2
    echo "       (fail-closed: refusing to treat this checkout as synced)" >&2
    return 1
  fi

  # A freshly (re)initialized repo has both no HEAD and an empty index.
  # `verify_synced_to` no longer cares about that (it reads the target tree
  # and the real filesystem directly, never the index — see its own header
  # comment for the production incident that made that necessary), but
  # HEAD/the index are still worth populating here for their own sake: a
  # repo with no HEAD at all can't answer basic `git log`/`git show`
  # questions an operator debugging the node over SSM would reasonably
  # expect to work, and leaves the checkout looking permanently "broken" to
  # anything that does still ask Git directly. `git checkout "${sha}" -- .`
  # populates the index AND working tree for every path tracked at `sha`,
  # repo-wide, in one step — while remaining exactly as safe as the
  # existing narrow `git checkout "${sha}" -- k8s` / `-- scripts` calls
  # elsewhere in this file, for the same reason: a checkout of an explicit
  # pathspec only ever writes paths git tracks at that commit, so it can
  # never touch the untracked k8s/overlays/aws/secrets/*.env files or
  # anything else not tracked at `sha`. This runs only on the repair path
  # (a repo that had no usable HEAD a moment ago), never on an
  # already-valid checkout.
  if ! git -C "${REPO_DIR}" rev-parse --verify -q HEAD >/dev/null 2>&1; then
    git -C "${REPO_DIR}" checkout --quiet "${sha}" -- . 2>/dev/null || true
    git -C "${REPO_DIR}" update-ref refs/heads/master "${sha}" 2>/dev/null || true
    git -C "${REPO_DIR}" symbolic-ref HEAD refs/heads/master 2>/dev/null || true
  fi
}

# A rendered manifest containing this string went through no substitution
# at all — reject it outright rather than trying to guess what URL was
# meant. Deliberately permissive about the host part (private IPs, Service
# DNS names, and public hostnames must all be valid here).
validate_upstream_url() {
  local url="$1"
  case "${url}" in
    *PLACEHOLDER*|"")
      echo "ERROR: SENTINEL_API_UPSTREAM is unset or still a placeholder: '${url}'" >&2
      return 1
      ;;
    http://*|https://*) ;;
    *)
      echo "ERROR: SENTINEL_API_UPSTREAM must be an http(s) URL, got: '${url}'" >&2
      return 1
      ;;
  esac
}

# Fails loudly if `file` still contains an unsubstituted template token.
# Split out of render_and_apply so the exact same check can run in
# scripts/tests/test_sentinel_deploy_contract.sh without any AWS/k8s
# access.
assert_no_placeholders() {
  local file="$1"
  if grep -qE 'PLACEHOLDER|ACCOUNT_ID\.dkr\.ecr' "${file}"; then
    echo "ERROR: unsubstituted placeholders remain in the rendered manifests:" >&2
    grep -nE 'PLACEHOLDER|ACCOUNT_ID\.dkr\.ecr' "${file}" >&2
    return 1
  fi
}

# Shared by "apply" and "apply-manifests": render k8s/overlays/aws with
# Kustomize, substitute the four placeholders with values discovered from
# this instance (never hardcoded), fail loudly if any placeholder survives
# OR if the value substituted in is not one a real deploy should ever ship
# with, then apply the rendered output. Assumes ${REPO_DIR}/k8s is already
# at the manifests the caller wants applied — it does not check anything
# out itself. Must be run from ${REPO_DIR}.
render_and_apply() {
  local sha="$1"
  local token aws_region aws_account_id public_ip ecr_registry sentinel_api_upstream rendered

  token="$(curl -sS -X PUT 'http://169.254.169.254/latest/api/token' \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')"
  aws_region="$(curl -sS -H "X-aws-ec2-metadata-token: ${token}" \
    http://169.254.169.254/latest/meta-data/placement/region)"
  aws_account_id="$(aws sts get-caller-identity --query Account --output text --region "${aws_region}")"
  public_ip="$(curl -sS -H "X-aws-ec2-metadata-token: ${token}" \
    http://169.254.169.254/latest/meta-data/public-ipv4)"
  ecr_registry="${aws_account_id}.dkr.ecr.${aws_region}.amazonaws.com"
  # Same default as deploy-aws.sh: the in-cluster topology's Service DNS
  # name. Set SENTINEL_API_UPSTREAM in the environment before calling this
  # for the external-control-plane topology — see docs/aws-deployment.md.
  # CI's automated path (ci-cd.yml's deploy-to-k3s job) now exports this
  # from the optional SENTINEL_API_UPSTREAM repository variable so the
  # external topology is covered by ordinary pushes too, not just a manual
  # operator invocation.
  sentinel_api_upstream="${SENTINEL_API_UPSTREAM:-http://sentinel-ai:8080}"
  validate_upstream_url "${sentinel_api_upstream}" || return 1

  rendered="$(mktemp)"
  trap 'rm -f "${rendered}"' RETURN
  kubectl kustomize k8s/overlays/aws \
    | sed -e "s|ACCOUNT_ID\.dkr\.ecr\.REGION\.amazonaws\.com|${ecr_registry}|g" \
          -e "s|:PLACEHOLDER|:${sha}|g" \
          -e "s|PUBLIC_IP_PLACEHOLDER|${public_ip}|g" \
          -e "s|SENTINEL_API_UPSTREAM_PLACEHOLDER|${sentinel_api_upstream}|g" \
    > "${rendered}"

  # Fail-loud guard: a survived placeholder must stop the deploy, not
  # silently reach the cluster.
  assert_no_placeholders "${rendered}" || return 1

  # Positive assertion, not just an absence-of-placeholder one: prove the
  # value we actually meant to ship is present verbatim. This is what would
  # have caught the historical incident even if some future edit renamed
  # the placeholder token without updating this sed list to match — a
  # mismatch there leaves no "PLACEHOLDER" string behind for the check
  # above to catch, since the raw base manifest's own literal is gone too,
  # replaced by nothing.
  if ! grep -qF "value: ${sentinel_api_upstream}" "${rendered}"; then
    echo "ERROR: rendered manifests do not contain the expected Sentinel endpoint" >&2
    echo "       (value: ${sentinel_api_upstream}) — refusing to apply." >&2
    return 1
  fi

  kubectl apply -f "${rendered}"
}

# Dispatch is wrapped in main() — invoked only when this file is executed
# directly (the only way it is ever actually used: by SSM, by CI, or by an
# operator running it by hand) — and NOT when it is sourced. That lets
# scripts/tests/test_sentinel_deploy_contract.sh source this file to
# exercise verify_executable/verify_synced_to/validate_upstream_url/
# assert_no_placeholders in isolation, with no AWS credentials, no
# kubeconfig, and no risk of accidentally deploying anything: sourcing this
# file does nothing beyond defining the functions above.
main() {
  MODE="${1:?usage: sentinel-deploy.sh <images|apply|sync-manifests|apply-manifests|sync-scripts|sync> <git-sha>}"
  SHA="${2:?usage: sentinel-deploy.sh <images|apply|sync-manifests|apply-manifests|sync-scripts|sync> <git-sha>}"

case "${MODE}" in
  images)
    # ECR_REGISTRY is discovered at runtime (account id from STS, region
    # from IMDS) rather than baked in, the same way scripts/deploy-aws.sh
    # does it — nothing here hardcodes an account id.
    token="$(curl -sS -X PUT 'http://169.254.169.254/latest/api/token' \
      -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')"
    AWS_REGION="$(curl -sS -H "X-aws-ec2-metadata-token: ${token}" \
      http://169.254.169.254/latest/meta-data/placement/region)"
    AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text --region "${AWS_REGION}")"
    REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

    # "kubectl set image" only updates a Deployment that already exists, and
    # its error when one does not is not obvious. The very first deploy of a
    # fresh cluster has to create the resources, which is deploy-aws.sh's
    # job, so fail here with the actual instruction rather than letting CI
    # report "deployments.apps not found".
    if ! kubectl -n "${NS}" get deployment citizen-service >/dev/null 2>&1; then
      echo "ERROR: the citizen-portal Deployments do not exist yet." >&2
      echo "The first deploy must create them:" >&2
      echo "  sudo ${REPO_DIR}/scripts/generate-aws-secrets.sh" >&2
      echo "  sudo ${REPO_DIR}/scripts/deploy-aws.sh ${SHA}" >&2
      echo "Subsequent CI deploys can then use this 'images' mode." >&2
      exit 1
    fi

    # Init containers share the service image, so they are set too —
    # otherwise the migration job would run the previous version's code.
    kubectl -n "${NS}" set image deployment/citizen-service \
      citizen-service="${REGISTRY}/${PREFIX}/citizen-service:${SHA}" \
      migrate-and-seed="${REGISTRY}/${PREFIX}/citizen-service:${SHA}"
    kubectl -n "${NS}" set image deployment/notification-service \
      notification-service="${REGISTRY}/${PREFIX}/notification-service:${SHA}" \
      migrate="${REGISTRY}/${PREFIX}/notification-service:${SHA}"
    kubectl -n "${NS}" set image deployment/frontend \
      frontend="${REGISTRY}/${PREFIX}/frontend:${SHA}"
    kubectl -n "${NS}" set image deployment/sentinel-ai \
      sentinel-ai="${REGISTRY}/${PREFIX}/sentinel-ai:${SHA}" || \
      echo "note: sentinel-ai deployment not present yet, skipped"
    # sentinel-gui is NOT set here any more: it has moved to the external
    # Sentinel EC2 as part of the Sentinel control plane (see
    # infra/terraform/sentinel_remote.tf) and is never deployed in K3s in
    # either topology. See .github/workflows/ci-cd.yml's deploy-to-sentinel
    # job for how its image gets updated instead.
    ;;
  apply)
    ensure_repo_synced "${SHA}" || exit 1
    cd "${REPO_DIR}"
    git checkout --detach "${SHA}"

    # Render-then-substitute, exactly like deploy-aws.sh — and for exactly
    # the same reason: `kubectl kustomize k8s/overlays/aws` on its own still
    # contains ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com, :PLACEHOLDER,
    # PUBLIC_IP_PLACEHOLDER and SENTINEL_API_UPSTREAM_PLACEHOLDER — a bare
    # `kubectl apply -k` of it (what this mode used to do) ships every one
    # of those literally to the cluster: unpullable images
    # (Init:InvalidImageName, since an "ACCOUNT_ID" host segment containing
    # an underscore is not even a syntactically valid image reference,
    # never mind one that resolves), a CORS origin of literally
    # "PUBLIC_IP_PLACEHOLDER", and an Alertmanager webhook that can never
    # reach Sentinel at all — alerts fire and never become incidents, and
    # nothing about that looks like a deploy failure. This mode must not
    # skip the substitution step deploy-aws.sh already gets right.
    if ! render_and_apply "${SHA}"; then
      exit 1
    fi

    # A manifest-changing deploy is exactly the moment to also refresh this
    # script from the checkout, since the checkout is already at ${SHA}.
    # Best-effort: a missing scripts/sentinel-deploy.sh (very old SHA)
    # should not fail an otherwise-successful manifest apply.
    if [ -f scripts/sentinel-deploy.sh ]; then
      install -m 0755 scripts/sentinel-deploy.sh /usr/local/bin/sentinel-deploy.sh
      verify_executable /usr/local/bin/sentinel-deploy.sh
    fi
    ;;
  sync-manifests)
    # CI-automated, narrower sibling of "apply": refresh ONLY tracked files
    # under k8s/ from the checkout at ${SHA}. `git checkout <sha> -- k8s`
    # writes exactly the tracked paths under k8s/ and nothing outside it —
    # no citizen-service/, no infra/terraform/, no HEAD move. Because git
    # checkout of a pathspec only ever touches paths git tracks, and
    # k8s/overlays/aws/secrets/*.env is gitignored (untracked — only the
    # *.env.example templates are tracked), this can never overwrite the
    # real, live secret values already on the node. Run apply-manifests
    # immediately after this to actually apply what was just synced.
    ensure_repo_synced "${SHA}" || exit 1
    cd "${REPO_DIR}"
    git checkout --quiet "${SHA}" -- k8s
    verify_synced_to "${SHA}" k8s
    echo "k8s manifests synced to ${SHA} (secrets/*.env untouched — untracked):"
    git show --stat --oneline "${SHA}" -- k8s | head -n -1 || true
    ;;
  apply-manifests)
    # Render+substitute+apply whatever is currently on disk at
    # ${REPO_DIR}/k8s/overlays/aws. Deliberately does NOT check anything out
    # itself — pair this with sync-manifests immediately before it (CI
    # always calls them back to back; see ci-cd.yml's deploy-to-k3s job).
    cd "${REPO_DIR}"
    if ! render_and_apply "${SHA}"; then
      exit 1
    fi
    ;;
  sync-scripts)
    # Fully replace scripts/ from the checkout at ${SHA} — additions,
    # edits, AND deletions (see the header comment for why this is safe
    # here but not for k8s/). Then reinstall the deploy script itself, same
    # as the original narrow "sync" mode did.
    ensure_repo_synced "${SHA}" || exit 1
    cd "${REPO_DIR}"
    rm -rf scripts
    git archive "${SHA}" -- scripts | tar -x
    # --exact: scripts/ has no untracked subtree (unlike k8s/), and was
    # just rm -rf'd above, so nothing should be here that isn't in the
    # target tree — verify that too, not just that the target tree's files
    # are present and correct.
    verify_synced_to --exact "${SHA}" scripts

    # `git archive | tar -x` preserves the executable bit git recorded for
    # each blob (100755 vs 100644) when this runs as root, which SSM's
    # AWS-RunShellScript document does — so in the ordinary case this
    # chmod is a no-op. It is made explicit anyway rather than relied on
    # implicitly: a file that was ever committed with the wrong mode (e.g.
    # added via a client that doesn't preserve +x, or edited through
    # something that reset it) would otherwise silently ship non-executable
    # and only be discovered the next time something tries to run it — see
    # docs/aws-deployment.md, "Deployment synchronization contract". Every
    # script here is invoked directly (by CI, by the chaos-scenario runner,
    # or by an operator over SSM), never sourced, so +x on all of them is
    # correct; nothing under scripts/ is a non-executable helper.
    chmod +x scripts/*.sh
    if [ -f scripts/sentinel-deploy.sh ]; then
      verify_executable scripts/sentinel-deploy.sh
      install -m 0755 scripts/sentinel-deploy.sh /usr/local/bin/sentinel-deploy.sh
      verify_executable /usr/local/bin/sentinel-deploy.sh
    fi
    echo "scripts/ synced to ${SHA} (including deletions), executable bits verified"
    ;;
  sync)
    # Refresh /usr/local/bin/sentinel-deploy.sh itself from the repository
    # checkout at ${SHA}, without touching any Kubernetes resources. See the
    # header comment above for why this exists. Deliberately narrow: this
    # touches exactly one file and runs no application code.
    ensure_repo_synced "${SHA}" || exit 1
    cd "${REPO_DIR}"
    git checkout --quiet "${SHA}" -- scripts/sentinel-deploy.sh
    verify_synced_to --exact "${SHA}" scripts/sentinel-deploy.sh
    chmod +x scripts/sentinel-deploy.sh
    install -m 0755 scripts/sentinel-deploy.sh /usr/local/bin/sentinel-deploy.sh
    verify_executable /usr/local/bin/sentinel-deploy.sh
    echo "sentinel-deploy.sh synced to ${SHA}"
    ;;
  *)
    echo "unknown mode: ${MODE}" >&2
    exit 2
    ;;
esac

# Deliberately NOT waiting for rollout success here (images/apply/
# apply-manifests modes). A bad deployment is a scenario this project
# exists to demonstrate: CI's job is to deploy, and Sentinel's job is to
# notice and remediate. Blocking CI on rollout status would mask exactly
# the failure mode we want observed.
  if [ "${MODE}" != "sync" ] && [ "${MODE}" != "sync-manifests" ] && [ "${MODE}" != "sync-scripts" ]; then
    kubectl -n "${NS}" get deployments -o wide
  fi
}

if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then
  main "$@"
fi
