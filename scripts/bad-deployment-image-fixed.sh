#!/usr/bin/env bash
# Phase 12 — image-backed bad-deployment scenario for the AWS/K3s demo.
#
# IMPORTANT: Run this script from a developer workstation/WSL, NOT on the K3s
# EC2 node. The project docs deliberately keep Docker off the node: K3s pulls
# images with containerd. This script builds/pushes the chaos image locally,
# then uses SSM to ask the node's Kubernetes API to deploy it.
#
# What it does:
#   1. Reads the currently deployed citizen-service image through SSM.
#   2. Builds a NEW image digest from that image with only harmless OCI labels.
#      The application behavior is unchanged; the image reference/digest is new.
#   3. Pushes the image to the SAME ECR repository under a unique chaos tag.
#   4. Atomically changes the citizen-service Pod template to:
#        - use the new image for both init + app containers;
#        - point DATABASE_HOST at a guaranteed-nonexistent host.
#      This creates one new ReplicaSet and makes image_changed=True for the
#      existing Sentinel correlation/RCA logic.
#   5. Leaves the release broken on purpose. Sentinel is expected to roll it back.
#
# Usage:
#   ./scripts/bad-deployment-image.sh --instance-id i-xxxxxxxxxxxxxxxxx
#   ./scripts/bad-deployment-image.sh --instance-id i-xxxxxxxxxxxxxxxxx --namespace citizen-portal
#   ./scripts/bad-deployment-image.sh --recover --instance-id i-xxxxxxxxxxxxxxxxx
#
# Requirements on WSL/workstation:
#   - aws CLI configured with ECR + SSM permissions
#   - docker
#   - python3
#   - git (used only to make the chaos tag traceable)
#
# Requirements on the EC2 node:
#   - SSM managed instance
#   - kubectl configured for K3s at /etc/rancher/k3s/k3s.yaml
set -euo pipefail

NAMESPACE="citizen-portal"
INSTANCE_ID=""
RECOVER="false"
BAD_DB_HOST="citizen-postgres-does-not-exist.invalid"
AWS_REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || true)}"

usage() {
  cat >&2 <<'USAGE'
Usage:
  bad-deployment-image.sh --instance-id i-xxxxxxxxxxxxxxxxx [--namespace citizen-portal]
  bad-deployment-image.sh --recover --instance-id i-xxxxxxxxxxxxxxxxx [--namespace citizen-portal]

Runs from WSL/developer workstation, not the K3s EC2 node.
USAGE
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

wait_for_ssm() {
  local command_id="$1"
  local status
  for _ in $(seq 1 60); do
    status=$(aws ssm get-command-invocation \
      --command-id "$command_id" \
      --instance-id "$INSTANCE_ID" \
      --query 'Status' \
      --output text 2>/dev/null || true)
    case "$status" in
      Success|Failed|Cancelled|TimedOut|Cancelling|Undeliverable|Terminated)
        printf '%s' "$status"
        return 0
        ;;
    esac
    sleep 2
  done
  printf '%s' "Timeout"
}

ssm_run() {
  local remote_script="$1"
  local params command_id status output stderr

  params=$(python3 - "$remote_script" <<'PY'
import json
import sys
print(json.dumps({"commands": [sys.argv[1]]}))
PY
)

  command_id=$(aws ssm send-command \
    --instance-ids "$INSTANCE_ID" \
    --document-name AWS-RunShellScript \
    --parameters "$params" \
    --comment "Sentinel SRE chaos: bad deployment image" \
    --query 'Command.CommandId' \
    --output text)

  status=$(wait_for_ssm "$command_id")
  output=$(aws ssm get-command-invocation \
    --command-id "$command_id" \
    --instance-id "$INSTANCE_ID" \
    --query 'StandardOutputContent' \
    --output text 2>/dev/null || true)
  printf '%s\n' "$output"

  if [ "$status" != "Success" ]; then
    stderr=$(aws ssm get-command-invocation \
      --command-id "$command_id" \
      --instance-id "$INSTANCE_ID" \
      --query 'StandardErrorContent' \
      --output text 2>/dev/null || true)
    [ -n "$stderr" ] && printf '%s\n' "$stderr" >&2
    fail "SSM command finished with status: $status"
  fi
}

get_live_image() {
  local remote_script
  remote_script=$(cat <<'REMOTE'
set -e
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl -n "__NAMESPACE__" get deployment citizen-service \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="citizen-service")].image}'
REMOTE
)
  remote_script=${remote_script//__NAMESPACE__/$NAMESPACE}
  ssm_run "$remote_script" | tail -n 1 | tr -d '\r' | tr -d '\n'
}

recover() {
  echo "=== Recovering citizen-service with rollout undo ==="
  local remote_script
  remote_script=$(cat <<'REMOTE'
set -e
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl -n "__NAMESPACE__" rollout undo deployment/citizen-service
kubectl -n "__NAMESPACE__" rollout status deployment/citizen-service --timeout=180s
kubectl -n "__NAMESPACE__" get deployment/citizen-service -o wide
kubectl -n "__NAMESPACE__" get pods -l app=citizen-service -o wide
REMOTE
)
  remote_script=${remote_script//__NAMESPACE__/$NAMESPACE}
  ssm_run "$remote_script"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --instance-id)
      [ "$#" -ge 2 ] || fail "--instance-id needs a value"
      INSTANCE_ID="$2"
      shift 2
      ;;
    --namespace)
      [ "$#" -ge 2 ] || fail "--namespace needs a value"
      NAMESPACE="$2"
      shift 2
      ;;
    --recover)
      RECOVER="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

require_cmd aws
require_cmd python3
[ -n "$INSTANCE_ID" ] || fail "--instance-id is required"

if [ "$RECOVER" = "true" ]; then
  recover
  exit 0
fi

require_cmd docker
require_cmd git
[ -n "$AWS_REGION" ] || fail "AWS_REGION is not set and no AWS CLI default region is configured"

echo "=== Sentinel SRE: image-backed bad-deployment ==="
echo "    Instance : $INSTANCE_ID"
echo "    Namespace: $NAMESPACE"
echo "    Region   : $AWS_REGION"
echo

echo "[1/6] Reading the live citizen-service image from the EC2 node..."
BASE_IMAGE="$(get_live_image)"
[ -n "$BASE_IMAGE" ] || fail "could not read the live citizen-service image from the cluster"
echo "    Live image: $BASE_IMAGE"

case "$BASE_IMAGE" in
  */*) ;;
  *) fail "live image does not look like a registry image: $BASE_IMAGE" ;;
esac

REGISTRY="${BASE_IMAGE%%/*}"
IMAGE_REPO_WITHOUT_TAG="${BASE_IMAGE%:*}"
[ "$IMAGE_REPO_WITHOUT_TAG" != "$BASE_IMAGE" ] || fail "live image must use a tag, not a digest"
IMAGE_REPO="${IMAGE_REPO_WITHOUT_TAG#*/}"

case "$IMAGE_REPO" in
  */citizen-service) ;;
  *) fail "expected the live image repo to end in /citizen-service, got: $IMAGE_REPO" ;;
esac

SHORT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
CHAOS_TAG="chaos-baddeployment-${SHORT_SHA}-$(date -u +%Y%m%d%H%M%S)"
BAD_IMAGE="${REGISTRY}/${IMAGE_REPO}:${CHAOS_TAG}"

echo "    New image:  $BAD_IMAGE"
echo

echo "[2/6] Logging in to ECR and pulling the current image..."
ECR_PASSWORD_FILE="$(mktemp)"
cleanup_ecr_password() { rm -f "$ECR_PASSWORD_FILE"; }
trap cleanup_ecr_password EXIT

aws ecr get-login-password --region "$AWS_REGION" > "$ECR_PASSWORD_FILE" \
  || fail "could not obtain ECR login password; check AWS credentials/permissions"

if ! docker login --username AWS --password-stdin "$REGISTRY" < "$ECR_PASSWORD_FILE"; then
  fail "Docker login to ECR failed. Run: docker info, then retry the login manually to see the Docker error."
fi
rm -f "$ECR_PASSWORD_FILE"
trap - EXIT

docker pull "$BASE_IMAGE"
echo "    OK — base image available locally"
echo

echo "[3/6] Building a new image digest (no application behavior change)..."
TMP_DIR="$(mktemp -d)"
cleanup_tmp() { rm -rf "$TMP_DIR"; }
trap cleanup_tmp EXIT
cat >"$TMP_DIR/Dockerfile" <<'DOCKERFILE'
FROM __BASE_IMAGE__
LABEL sentinel.sre.chaos.scenario="bad-deployment-image"
LABEL sentinel.sre.chaos.generated-at="__CHAOS_TAG__"
DOCKERFILE
sed -i "s|__BASE_IMAGE__|$BASE_IMAGE|; s|__CHAOS_TAG__|$CHAOS_TAG|" "$TMP_DIR/Dockerfile"

docker build --pull=false -t "$BAD_IMAGE" "$TMP_DIR"
echo "    OK — new image built"
echo

echo "[4/6] Pushing the chaos image to ECR..."
docker push "$BAD_IMAGE"
echo "    OK — image pushed"
echo

echo "[5/6] Applying ONE Kubernetes Pod-template change through SSM..."
echo "    Image:         ${BASE_IMAGE} -> ${BAD_IMAGE}"
echo "    DATABASE_HOST: -> ${BAD_DB_HOST}"

PATCH_JSON=$(python3 - "$BAD_IMAGE" "$BAD_DB_HOST" <<'PY'
import json
import sys
image, db_host = sys.argv[1:]
patch = {
    "spec": {
        "template": {
            "spec": {
                "initContainers": [{
                    "name": "migrate-and-seed",
                    "image": image,
                    "env": [{"name": "DATABASE_HOST", "value": db_host}],
                }],
                "containers": [{
                    "name": "citizen-service",
                    "image": image,
                    "env": [{"name": "DATABASE_HOST", "value": db_host}],
                }],
            }
        }
    }
}
print(json.dumps(patch, separators=(",", ":")))
PY
)

PATCH_QUOTED=$(printf '%q' "$PATCH_JSON")
NS_QUOTED=$(printf '%q' "$NAMESPACE")
REMOTE_APPLY=$(cat <<REMOTE
set -e
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
NS=$NS_QUOTED
DEPLOY=citizen-service
BEFORE=\$(kubectl -n "\$NS" get deployment "\$DEPLOY" -o jsonpath='{.metadata.annotations.deployment\\.kubernetes\\.io/revision}')
echo "Good revision: \$BEFORE"
kubectl -n "\$NS" patch deployment "\$DEPLOY" --type=strategic --patch $PATCH_QUOTED
AFTER=\$(kubectl -n "\$NS" get deployment "\$DEPLOY" -o jsonpath='{.metadata.annotations.deployment\\.kubernetes\\.io/revision}')
echo "Bad revision:  \$AFTER"
kubectl -n "\$NS" rollout history deployment/"\$DEPLOY"
echo
kubectl -n "\$NS" get rs -l app="\$DEPLOY" -o wide
echo
kubectl -n "\$NS" get pods -l app="\$DEPLOY" -o wide
REMOTE
)
ssm_run "$REMOTE_APPLY"

echo
echo "[6/6] Injection complete — the release is intentionally left broken."
echo
echo "Expected Sentinel behavior:"
echo "  1. detect ServiceDown / stalled rollout"
echo "  2. collect Prometheus + Loki + Kubernetes evidence"
echo "  3. RCA: bad_deployment with image_changed=true"
echo "  4. choose rollback_deployment"
echo "  5. Policy should see confidence 0.96 vs rollback threshold 0.95"
echo "  6. perform rollout undo"
echo "  7. validate recovery"
echo
echo "To recover manually (only if Sentinel does not):"
echo "  $0 --recover --instance-id $INSTANCE_ID --namespace $NAMESPACE"
echo
echo "To watch the cluster in your existing SSM session:"
echo "  kubectl -n $NAMESPACE get rs -l app=citizen-service -w"
echo "  kubectl -n $NAMESPACE get pods -l app=citizen-service -w"
echo "  kubectl -n $NAMESPACE rollout history deployment/citizen-service"
echo
echo "To inspect Sentinel's live decisions, use the Sentinel GUI / incident timeline."
