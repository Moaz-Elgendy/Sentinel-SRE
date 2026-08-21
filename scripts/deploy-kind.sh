#!/bin/bash
# Phase 8 — deploy the full stack to a local kind cluster, end to end.
#
# What this does, in order:
#   1. Create (or reuse) a kind cluster using k8s/kind-config.yaml
#   2. Install ingress-nginx (kind-specific manifest)
#   3. Build the three application images
#   4. Load them into the kind cluster's image store
#   5. Apply every manifest in k8s/ via Kustomize
#   6. Wait for every Deployment to become available
#   7. Print the /etc/hosts line you need and a curl smoke test
#
# Prerequisites: docker, kind, kubectl all installed and on PATH.
# Safe to re-run — every step is idempotent (kind reuses an existing
# cluster with the same name, kubectl apply is declarative).
set -euo pipefail

# NOTE: ingress-nginx (the kubernetes/ingress-nginx project) was retired by
# the Kubernetes Steering and Security Response Committees in March 2026 —
# no further releases, bugfixes, or security patches. It still installs and
# runs fine for a local kind/minikube demo (this script pins the last
# maintained release, v1.15.1), but this is NOT a safe choice for anything
# beyond local experimentation. See k8s/README.md "Ingress controller
# note" for real alternatives (Traefik, F5 NGINX Ingress Controller,
# Gateway API implementations) before this ever runs anywhere internet-facing.

CLUSTER_NAME="citizen-portal"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> [1/7] Checking prerequisites"
for bin in docker kind kubectl; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "ERROR: '$bin' not found on PATH. Install it before running this script." >&2
    exit 1
  fi
done

echo "==> [2/7] Creating kind cluster '$CLUSTER_NAME' (skipping if it already exists)"
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  echo "    Cluster '$CLUSTER_NAME' already exists — reusing it."
else
  kind create cluster --config "$REPO_ROOT/k8s/kind-config.yaml"
fi

echo "==> [3/7] Installing ingress-nginx (kind-specific manifest, last maintained release — see note below)"
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.15.1/deploy/static/provider/kind/deploy.yaml
echo "    Waiting for the ingress-nginx controller to become ready (this can take a minute)..."
kubectl wait --namespace ingress-nginx \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller \
  --timeout=180s

echo "==> [4/7] Building application images"
docker build -t citizen-portal/citizen-service:latest "$REPO_ROOT/citizen-service"
docker build -t citizen-portal/notification-service:latest "$REPO_ROOT/notification-service"
# VITE_API_BASE_URL is intentionally empty here — see k8s/README.md for why
# this must differ from the docker-compose frontend build.
docker build -t citizen-portal/frontend:latest \
  --build-arg VITE_API_BASE_URL="" \
  "$REPO_ROOT/frontend"

echo "==> [5/7] Loading images into the kind cluster"
kind load docker-image citizen-portal/citizen-service:latest --name "$CLUSTER_NAME"
kind load docker-image citizen-portal/notification-service:latest --name "$CLUSTER_NAME"
kind load docker-image citizen-portal/frontend:latest --name "$CLUSTER_NAME"

echo "==> [6/7] Applying Kubernetes manifests"
kubectl apply -k "$REPO_ROOT/k8s/"

echo "==> [7/7] Waiting for Deployments to become available"
for dep in citizen-postgres notification-postgres citizen-service notification-service frontend prometheus alertmanager loki grafana; do
  echo "    Waiting for deployment/$dep..."
  kubectl rollout status deployment/"$dep" -n citizen-portal --timeout=180s
done
echo "    Waiting for daemonset/alloy..."
kubectl rollout status daemonset/alloy -n citizen-portal --timeout=180s

echo
echo "============================================================"
echo " Deploy complete."
echo "============================================================"
echo
echo "Add this line to /etc/hosts if it isn't there already:"
echo "  127.0.0.1  citizen-portal.local"
echo
echo "Then open: http://citizen-portal.local"
echo
echo "Quick smoke test once the hosts entry is in place:"
echo "  curl -s http://citizen-portal.local/api/services | head -c 300"
echo
echo "If that curl fails, see the Troubleshooting section in k8s/README.md,"
echo "or start with: kubectl get pods -n citizen-portal"
echo
echo "Observability stack (Prometheus/Grafana/Loki/Alertmanager) is up too,"
echo "reachable via kubectl port-forward — see 'Accessing the observability"
echo "stack' in k8s/README.md for the exact commands."
