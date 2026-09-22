#!/bin/bash
# Tears down whichever local cluster was used for Phase 8, deleting
# everything (including PVCs — this is a full reset, not just a pause).
#
# Usage: ./scripts/teardown.sh kind      (or)      ./scripts/teardown.sh minikube
set -euo pipefail

TARGET="${1:-}"

case "$TARGET" in
  kind)
    if command -v kind >/dev/null 2>&1 && kind get clusters 2>/dev/null | grep -qx "citizen-portal"; then
      kind delete cluster --name citizen-portal
      echo "kind cluster 'citizen-portal' deleted."
    else
      echo "No kind cluster named 'citizen-portal' found — nothing to do."
    fi
    ;;
  minikube)
    if command -v minikube >/dev/null 2>&1; then
      minikube delete
      echo "minikube cluster deleted."
    else
      echo "minikube not found — nothing to do."
    fi
    ;;
  *)
    echo "Usage: $0 kind|minikube" >&2
    exit 1
    ;;
esac
