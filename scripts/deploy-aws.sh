#!/usr/bin/env bash
#
# Deploy the citizen portal + observability stack + Sentinel to the K3s node.
#
# Runs ON the EC2 instance. Reach it over SSM:
#   aws ssm start-session --target <instance-id> --region <region>
#   sudo /opt/sentinel-sre/scripts/deploy-aws.sh <image-tag>
#
# <image-tag> is a git commit SHA that CI has already pushed to ECR. Use
# "latest" only for a throwaway manual test — a SHA is what makes the
# running ReplicaSet traceable back to a commit, which is what Sentinel's
# deployment correlation depends on.
#
# ---------------------------------------------------------------------------
# Why render-then-substitute rather than `kustomize edit set image`
# ---------------------------------------------------------------------------
# `kustomize edit` needs the standalone kustomize binary and mutates the
# tracked kustomization.yaml in place, which leaves the checkout dirty and
# makes the next `git checkout` conflict. `kubectl kustomize` (built into
# k3s's bundled kubectl) can render but not edit, so this renders and then
# substitutes three placeholders in the output stream. Nothing on disk is
# modified.
#
# The three placeholders and why each cannot be resolved at build time:
#   ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com  - depends on the AWS account
#   :PLACEHOLDER                             - depends on the commit deployed
#   PUBLIC_IP_PLACEHOLDER                    - depends on the instance's IP
#
# All three are deliberately invalid rather than plausible defaults, so a
# substitution that silently fails to happen produces an immediate,
# obvious error (ImagePullBackOff, or a CORS rejection in the browser)
# instead of quietly running the wrong thing.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OVERLAY="${REPO_ROOT}/k8s/overlays/aws"
NAMESPACE="citizen-portal"

export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"

IMAGE_TAG="${1:-}"
if [ -z "${IMAGE_TAG}" ]; then
  cat >&2 <<EOF
usage: $(basename "$0") <image-tag>

  <image-tag>  git commit SHA already pushed to ECR by CI, e.g. abc1234
               (or "latest" for a manual test)

Before the first run:
  sudo ${REPO_ROOT}/scripts/generate-aws-secrets.sh
EOF
  exit 1
fi

command -v kubectl >/dev/null 2>&1 || {
  echo "ERROR: kubectl not found. Is K3s installed? Check /var/log/sentinel-bootstrap.log" >&2
  exit 1
}

# ---------------------------------------------------------------------------
# Discover account, region and public IP from the instance itself
#
# Nothing is hardcoded and no credentials are needed: the account id comes
# from STS via the instance profile, and the region and public IP come from
# IMDS. IMDSv2 is enforced on this instance (see infra/terraform/ec2.tf), so
# the token dance is required, not optional.
# ---------------------------------------------------------------------------
imds() {
  local token
  token="$(curl -sS -X PUT 'http://169.254.169.254/latest/api/token' \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')"
  curl -sS -H "X-aws-ec2-metadata-token: ${token}" \
    "http://169.254.169.254/latest/meta-data/$1"
}

echo "--- discovering environment ---"
PUBLIC_IP="$(imds public-ipv4)"
AWS_REGION="$(imds placement/region)"
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text --region "${AWS_REGION}")"
ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

echo "  region     : ${AWS_REGION}"
echo "  account    : ${AWS_ACCOUNT_ID}"
echo "  public IP  : ${PUBLIC_IP}"
echo "  registry   : ${ECR_REGISTRY}"
echo "  image tag  : ${IMAGE_TAG}"

# ---------------------------------------------------------------------------
# Preflight: the secret env files must exist
#
# The overlay's secretGenerator reads them, so a missing file makes
# `kubectl kustomize` fail. Checking here gives a useful message instead of
# a Kustomize stack trace.
# ---------------------------------------------------------------------------
missing=()
for f in citizen-postgres notification-postgres citizen-service notification-service grafana sentinel; do
  [ -f "${OVERLAY}/secrets/${f}.env" ] || missing+=("${f}.env")
done
if [ ${#missing[@]} -gt 0 ]; then
  echo "ERROR: missing secret files in ${OVERLAY}/secrets/: ${missing[*]}" >&2
  echo "Run: sudo ${REPO_ROOT}/scripts/generate-aws-secrets.sh" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# ECR pull credentials
#
# Normally maintained by the refresh-ecr-credentials systemd timer. Force a
# refresh now so a deploy immediately after a long idle period does not hit
# an expired 12-hour token.
# ---------------------------------------------------------------------------
echo "--- refreshing ECR pull credentials ---"
systemctl start refresh-ecr-credentials.service 2>/dev/null \
  || /usr/local/bin/refresh-ecr-credentials.sh \
  || echo "WARNING: ECR credential refresh failed; image pulls may fail"

# ---------------------------------------------------------------------------
# Render, substitute, apply
# ---------------------------------------------------------------------------
echo "--- rendering overlay ---"
RENDERED="$(mktemp)"
trap 'rm -f "${RENDERED}"' EXIT

kubectl kustomize "${OVERLAY}" \
  | sed -e "s|ACCOUNT_ID\.dkr\.ecr\.REGION\.amazonaws\.com|${ECR_REGISTRY}|g" \
        -e "s|:PLACEHOLDER|:${IMAGE_TAG}|g" \
        -e "s|PUBLIC_IP_PLACEHOLDER|${PUBLIC_IP}|g" \
  > "${RENDERED}"

# Fail loudly if any placeholder survived. Without this check a typo in the
# sed expressions above would deploy an unresolvable image reference, and
# the failure would surface minutes later as ImagePullBackOff with no
# indication of why.
if grep -qE 'PLACEHOLDER|ACCOUNT_ID\.dkr\.ecr' "${RENDERED}"; then
  echo "ERROR: unsubstituted placeholders remain in the rendered manifests:" >&2
  grep -nE 'PLACEHOLDER|ACCOUNT_ID\.dkr\.ecr' "${RENDERED}" >&2
  exit 1
fi

echo "--- applying ---"
kubectl apply -f "${RENDERED}"

# ---------------------------------------------------------------------------
# Wait for rollout
#
# Postgres first: the app services' init containers run `alembic upgrade
# head` and will crash-loop until the database accepts connections. Waiting
# here turns that into an ordered startup rather than a few minutes of
# alarming-looking restarts.
# ---------------------------------------------------------------------------
echo "--- waiting for databases ---"
for d in citizen-postgres notification-postgres; do
  kubectl -n "${NAMESPACE}" rollout status "deployment/${d}" --timeout=300s
done

echo "--- waiting for application and observability stack ---"
for d in citizen-service notification-service frontend prometheus alertmanager loki grafana sentinel-ai; do
  # `|| true` deliberately: one component failing to become ready should not
  # abort the wait on the others. The verification block below reports the
  # real state, which is more useful than stopping at the first problem.
  kubectl -n "${NAMESPACE}" rollout status "deployment/${d}" --timeout=300s || {
    echo "WARNING: ${d} did not become ready within 300s"
  }
done

kubectl -n "${NAMESPACE}" rollout status daemonset/alloy --timeout=180s || true

# ---------------------------------------------------------------------------
# Report actual state — do not claim success
# ---------------------------------------------------------------------------
echo
echo "=== pods ==="
kubectl -n "${NAMESPACE}" get pods -o wide

echo
echo "=== deployments ==="
kubectl -n "${NAMESPACE}" get deployments

echo
echo "=== persistent volumes ==="
kubectl -n "${NAMESPACE}" get pvc

echo
echo "=== ingress ==="
kubectl -n "${NAMESPACE}" get ingress

not_ready="$(kubectl -n "${NAMESPACE}" get pods \
  --field-selector=status.phase!=Running,status.phase!=Succeeded \
  -o name 2>/dev/null | wc -l)"

echo
if [ "${not_ready}" -gt 0 ]; then
  echo "DEPLOY INCOMPLETE: ${not_ready} pod(s) are not Running."
  echo "Investigate with:"
  echo "  sudo kubectl -n ${NAMESPACE} get pods"
  echo "  sudo kubectl -n ${NAMESPACE} describe pod <name>"
  echo "  sudo kubectl -n ${NAMESPACE} logs <name> --previous"
  exit 1
fi

cat <<EOF
Applied successfully. All pods are Running.

That is NOT the same as the environment being verified. Still to check:
  curl -sS http://${PUBLIC_IP}/            -> frontend HTML
  curl -sS http://${PUBLIC_IP}/api/services -> seeded service list
  ${REPO_ROOT}/scripts/smoke-test.sh http://${PUBLIC_IP}

Then the observability stack and Sentinel — see docs/aws-deployment.md
steps 20-31.

Portal: http://${PUBLIC_IP}
EOF
