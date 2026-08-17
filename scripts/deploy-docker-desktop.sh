#!/bin/bash
# Phase 8 — deploy the full stack to Docker Desktop's built-in Kubernetes.
#
# Docker Desktop's Kubernetes is simpler to target than kind/minikube for
# this project: it's already running (no cluster-create step), and it
# shares Docker Desktop's own image store directly — an image built with
# `docker build` is immediately visible to the cluster, no
# `kind load docker-image` / `minikube image load` step needed at all.
#
# What this script does, in order:
#   1. Confirm kubectl is pointed at the docker-desktop context (refuses
#      to run otherwise, so it can never accidentally apply to a kind/
#      minikube/real cluster you happen to also have configured)
#   2. Install ingress-nginx (cloud provider manifest — Docker Desktop
#      natively supports LoadBalancer Services, binding them to localhost,
#      so no NodePort/extraPortMappings workaround is needed here)
#   3. Build the three application images (skip with --skip-build if
#      you've already built them yourself, matching this project's
#      image: names — see k8s/*/deployment.yaml)
#   4. Apply every manifest in k8s/ via Kustomize
#   5. Wait for every Deployment to become available
#   6. Print the /etc/hosts line and a smoke-test command
#
# Prerequisites: Docker Desktop with Kubernetes enabled, kubectl on PATH.
# Safe to re-run — every step is idempotent.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKIP_BUILD=false
if [[ "${1:-}" == "--skip-build" ]]; then
  SKIP_BUILD=true
fi

echo "==> [1/6] Checking prerequisites and kubectl context"
for bin in docker kubectl; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "ERROR: '$bin' not found on PATH. Install it before running this script." >&2
    exit 1
  fi
done

CURRENT_CONTEXT="$(kubectl config current-context 2>/dev/null || echo "")"
if [[ "$CURRENT_CONTEXT" != "docker-desktop" ]]; then
  echo "ERROR: kubectl's current context is '$CURRENT_CONTEXT', not 'docker-desktop'." >&2
  echo "       Run: kubectl config use-context docker-desktop" >&2
  echo "       (Refusing to continue — don't want to apply this to the wrong cluster.)" >&2
  exit 1
fi
echo "    OK — targeting docker-desktop"

echo "==> [2/6] Installing ingress-nginx (cloud provider manifest, last maintained release)"
# NOTE: kubernetes/ingress-nginx was retired by the Kubernetes Steering and
# Security Response Committees in March 2026 — no further releases or
# security patches. Still fine for local development; see
# k8s/README.md "Ingress controller note" before this goes anywhere
# internet-facing.
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.15.1/deploy/static/provider/cloud/deploy.yaml
echo "    Waiting for the ingress-nginx controller to become ready (this can take a minute)..."
kubectl wait --namespace ingress-nginx \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller \
  --timeout=180s

if [[ "$SKIP_BUILD" == "false" ]]; then
  echo "==> [3/6] Building application images"
  docker build -t citizen-service:latest "$REPO_ROOT/citizen-service"
  docker build -t notification-service:latest "$REPO_ROOT/notification-service"
  # VITE_API_BASE_URL is intentionally empty — see k8s/README.md for why
  # this must differ from the docker-compose frontend build.
  docker build -t frontend:latest \
    --build-arg VITE_API_BASE_URL="" \
    "$REPO_ROOT/frontend"
else
  echo "==> [3/6] Skipping image build (--skip-build passed) — using whatever is already tagged"
  echo "    citizen-service:latest, notification-service:latest, frontend:latest"
fi

echo "==> [4/6] Applying Kubernetes manifests"
kubectl apply -k "$REPO_ROOT/k8s/"

echo "==> [5/6] Waiting for Deployments to become available"
for dep in citizen-postgres notification-postgres citizen-service notification-service frontend prometheus alertmanager loki grafana; do
  echo "    Waiting for deployment/$dep..."
  kubectl rollout status deployment/"$dep" -n citizen-portal --timeout=180s
done
echo "    Waiting for daemonset/alloy..."
kubectl rollout status daemonset/alloy -n citizen-portal --timeout=180s

echo "==> [6/6] Done"
echo
echo "============================================================"
echo " Deploy complete."
echo "============================================================"
echo
echo "Add this line to your hosts file if it isn't there already"
echo "(Docker Desktop's LoadBalancer binds Ingress traffic to localhost):"
echo "  127.0.0.1  citizen-portal.local"
echo
echo "  macOS/Linux: /etc/hosts"
echo "  Windows:     C:\\Windows\\System32\\drivers\\etc\\hosts (edit as Administrator)"
echo
echo "Then open: http://citizen-portal.local"
echo
echo "Quick smoke test once the hosts entry is in place:"
echo "  ./scripts/smoke-test.sh"
echo
echo "If citizen-portal.local doesn't resolve or the page doesn't load, check:"
echo "  kubectl get svc -n ingress-nginx ingress-nginx-controller"
echo "  (EXTERNAL-IP should show 'localhost' for Docker Desktop)"
echo
echo "Observability stack (Prometheus/Grafana/Loki/Alertmanager) is up too,"
echo "reachable via kubectl port-forward — see 'Accessing the observability"
echo "stack' in k8s/README.md for the exact commands."
