#!/bin/bash
# Phase 8 — deploy the full stack to a local minikube cluster, end to end.
# Alternative to scripts/deploy-kind.sh — use whichever local cluster tool
# you already have installed.
#
# What this does, in order:
#   1. Start minikube (if not already running)
#   2. Enable the ingress addon
#   3. Build the three application images
#   4. Load them into minikube's image store
#   5. Apply every manifest in k8s/ via Kustomize
#   6. Wait for every Deployment to become available
#   7. Print the /etc/hosts line you need and a curl smoke test
#
# Prerequisites: docker, minikube, kubectl all installed and on PATH.
set -euo pipefail

# NOTE: minikube's `ingress` addon bundles ingress-nginx. As of March 2026
# the upstream kubernetes/ingress-nginx project is retired (no more
# releases/security patches) — see k8s/README.md "Ingress controller note".
# minikube's addon still installs and works for local development, but
# don't treat this as safe for anything beyond local experimentation.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> [1/7] Checking prerequisites"
for bin in docker minikube kubectl; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "ERROR: '$bin' not found on PATH. Install it before running this script." >&2
    exit 1
  fi
done

echo "==> [2/7] Starting minikube (skipping if already running)"
if minikube status >/dev/null 2>&1; then
  echo "    minikube is already running — reusing it."
else
  minikube start
fi

echo "==> [3/7] Enabling the ingress addon"
minikube addons enable ingress
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

echo "==> [5/7] Loading images into minikube"
minikube image load citizen-portal/citizen-service:latest
minikube image load citizen-portal/notification-service:latest
minikube image load citizen-portal/frontend:latest

echo "==> [6/7] Applying Kubernetes manifests"
kubectl apply -k "$REPO_ROOT/k8s/"

echo "==> [7/7] Waiting for Deployments to become available"
for dep in citizen-postgres notification-postgres citizen-service notification-service frontend prometheus alertmanager loki grafana; do
  echo "    Waiting for deployment/$dep..."
  kubectl rollout status deployment/"$dep" -n citizen-portal --timeout=180s
done
echo "    Waiting for daemonset/alloy..."
kubectl rollout status daemonset/alloy -n citizen-portal --timeout=180s

MINIKUBE_IP="$(minikube ip)"

echo
echo "============================================================"
echo " Deploy complete."
echo "============================================================"
echo
echo "Add this line to /etc/hosts (minikube's IP, not 127.0.0.1):"
echo "  $MINIKUBE_IP  citizen-portal.local"
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
echo
echo "Note: minikube's IP can change across 'minikube start' calls — if"
echo "the hosts entry stops resolving after a restart, re-run 'minikube ip'"
echo "and update /etc/hosts."
