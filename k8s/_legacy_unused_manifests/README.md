# Deprecated — not applied by anything

These manifests were the pre-Kustomize `k8s/*` layout described in the
"Why the manifests moved into `base/`" section of `k8s/README.md`. When the
project moved to `k8s/base/` + `k8s/overlays/{local,aws}`, these files were
left behind instead of being deleted.

Verified during the September 2026 engineering audit that nothing applies
them any more:
  * `k8s/kustomization.yaml` only resources `overlays/local`, which only
    resources `../../base` — never these paths.
  * `k8s/overlays/aws/kustomization.yaml` only resources `../../base`.
  * Every deploy script (`scripts/deploy-kind.sh`, `deploy-docker-desktop.sh`,
    `deploy-minikube.sh`, `deploy-aws.sh`) runs `kubectl apply -k` against
    `k8s/` or `k8s/overlays/aws`, never a path under here.
  * `k8s/README.md` and CI already document/reference only `k8s/base/*`.

Their content is otherwise identical to `k8s/base/*` (same manifests, just
CRLF line endings). Moved here rather than deleted so nothing is lost;
safe to delete this whole directory once you've confirmed you don't need
it for reference.
