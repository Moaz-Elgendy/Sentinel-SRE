#!/usr/bin/env bash
# Deploy a specific image tag to the citizen portal namespace on the K3s
# node, or refresh this script itself on the node.
#
# Usage:
#   sentinel-deploy.sh images <git-sha>   # roll the app/Sentinel images to <git-sha>
#   sentinel-deploy.sh apply <git-sha>    # git checkout <git-sha> + kubectl apply -k
#   sentinel-deploy.sh sync  <git-sha>    # refresh THIS script on the node from <git-sha>
#
# "images" is the normal CI path: it changes only the container images,
# which produces a clean new ReplicaSet and therefore real rollout history
# for Sentinel to correlate against and roll back to.
#
# "apply" is for manifest changes and requires the checkout in ${REPO_DIR}.
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

MODE="${1:?usage: sentinel-deploy.sh <images|apply|sync> <git-sha>}"
SHA="${2:?usage: sentinel-deploy.sh <images|apply|sync> <git-sha>}"

# Fixed for this environment — mirrors infra/terraform/variables.tf's
# app_namespace/project_name defaults and ec2.tf's repo_dir. These three
# things do not vary independently of a full infrastructure redeploy, so
# they are constants here rather than plumbed through as arguments; that
# keeps the CI call site (`sentinel-deploy.sh images <sha>`) unchanged.
NS="citizen-portal"
PREFIX="sentinel-sre-demo"
REPO_DIR="/opt/sentinel-sre"

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
    # sentinel-gui follows the exact same pattern as sentinel-ai above: a
    # fresh/older cluster that has not deployed the sentinel-gui Deployment
    # yet (see k8s/overlays/aws/sentinel-gui/) must not break this step for
    # every other service.
    kubectl -n "${NS}" set image deployment/sentinel-gui \
      sentinel-gui="${REGISTRY}/${PREFIX}/sentinel-gui:${SHA}" || \
      echo "note: sentinel-gui deployment not present yet, skipped"
    ;;
  apply)
    cd "${REPO_DIR}"
    git fetch --all --tags
    git checkout --detach "${SHA}"
    kubectl apply -k k8s/overlays/aws
    # A manifest-changing deploy is exactly the moment to also refresh this
    # script from the checkout, since the checkout is already at ${SHA}.
    # Best-effort: a missing scripts/sentinel-deploy.sh (very old SHA)
    # should not fail an otherwise-successful manifest apply.
    if [ -f scripts/sentinel-deploy.sh ]; then
      install -m 0755 scripts/sentinel-deploy.sh /usr/local/bin/sentinel-deploy.sh
    fi
    ;;
  sync)
    # Refresh /usr/local/bin/sentinel-deploy.sh itself from the repository
    # checkout at ${SHA}, without touching any Kubernetes resources. See the
    # header comment above for why this exists. Deliberately narrow: this
    # touches exactly one file and runs no application code.
    if [ ! -d "${REPO_DIR}/.git" ]; then
      echo "ERROR: ${REPO_DIR} is not a git checkout; cannot sync." >&2
      echo "See docs/aws-deployment.md for populating it (private repo)." >&2
      exit 1
    fi
    cd "${REPO_DIR}"
    git fetch --all --tags --quiet
    git checkout --quiet "${SHA}" -- scripts/sentinel-deploy.sh
    install -m 0755 scripts/sentinel-deploy.sh /usr/local/bin/sentinel-deploy.sh
    echo "sentinel-deploy.sh synced to ${SHA}"
    ;;
  *)
    echo "unknown mode: ${MODE}" >&2
    exit 2
    ;;
esac

# Deliberately NOT waiting for rollout success here (images/apply modes). A
# bad deployment is a scenario this project exists to demonstrate: CI's job
# is to deploy, and Sentinel's job is to notice and remediate. Blocking CI
# on rollout status would mask exactly the failure mode we want observed.
if [ "${MODE}" != "sync" ]; then
  kubectl -n "${NS}" get deployments -o wide
fi
