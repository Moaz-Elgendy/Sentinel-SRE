# Kubernetes manifests (Phase 7)

Plain YAML manifests that run the same stack `docker compose up` brings up locally, on a
Kubernetes cluster. No Helm yet — see `Phases.md` for why.

**Phase 8 status: automation scripts are written and reviewed, but not yet run against a real
cluster in this environment** — no `docker`, `kind`, `minikube`, or `kubectl` were available in
the sandbox these were built in. Everything below has been checked as thoroughly as possible
without a cluster (see "Validating manifests without a cluster"), but you are the first person to
actually run `scripts/deploy-kind.sh` (or `deploy-minikube.sh`) for real. If something breaks on
that first run, it's expected — please report it back so it gets fixed here.

## Layout

```text
k8s/
├── kustomization.yaml        # kubectl apply -k k8s/  — applies everything below in order
├── kind-config.yaml           # kind cluster config (ingress-ready node, host port mappings)
├── namespace.yaml
├── postgres/                  # citizen-service's Postgres
│   ├── secret.yaml
│   ├── pvc.yaml
│   ├── deployment.yaml
│   └── service.yaml
├── notification-postgres/    # notification-service's own Postgres (one DB per microservice)
│   ├── secret.yaml
│   ├── pvc.yaml
│   ├── deployment.yaml
│   └── service.yaml
├── citizen-service/
│   ├── configmap.yaml         # non-secret env (DB host/port/name, JWT alg, CORS origins, ...)
│   ├── secret.yaml            # DATABASE_USER/PASSWORD, JWT_SECRET
│   ├── pvc.yaml                # /data/uploads
│   ├── deployment.yaml         # initContainer runs migrations + seed once per rollout
│   └── service.yaml            # ClusterIP :8000
├── notification-service/
│   ├── configmap.yaml
│   ├── secret.yaml
│   ├── deployment.yaml         # initContainer runs migrations
│   └── service.yaml            # ClusterIP :8000 — never exposed via Ingress, server-to-server only
├── frontend/
│   ├── deployment.yaml
│   └── service.yaml            # ClusterIP :3000
└── ingress/
    └── ingress.yaml            # one host, "/" -> frontend, "/api" -> citizen-service

scripts/
├── deploy-kind.sh              # full automated deploy to a local kind cluster
├── deploy-minikube.sh          # full automated deploy to a local minikube cluster
├── teardown.sh                 # tears down either cluster
└── smoke-test.sh                # end-to-end flow test against a live deployment
```

## Design decisions worth knowing about

- **One Postgres Deployment per service, not a shared instance.** Matches the
  one-database-per-microservice boundary from Phase 2/5 — `citizen-postgres` and
  `notification-postgres` are fully independent, each with their own PVC and Secret.
- **`replicas: 1` and `strategy: Recreate` on both Postgres Deployments.** These are stateful,
  backed by a single `ReadWriteOnce` PVC. A `Deployment` here (rather than a `StatefulSet`) is
  the simplest thing that works for a single-instance demo database — don't scale these up.
- **Migrations run in an `initContainer`, not the main container's start command.**
  `docker-compose.yml` runs `alembic upgrade head` as part of each container's boot command,
  which is fine at 1 replica. `citizen-service` and `notification-service` both run 2 replicas
  here — an `initContainer` runs the migration once per pod *before* it starts, and Alembic's own
  locking makes concurrent runs across the 2 pods' init containers safe either way, but there's
  no reason to pay for a migration attempt on every pod boot when a clean initContainer pattern
  reads better and does the same job.
- **`notification-service` has no Ingress rule.** It's only ever called server-to-server by
  `citizen-service` (see `citizen-service/app/core/notifications.py`) — never directly by a
  browser — so it stays `ClusterIP`-only, same as both Postgres instances.
- **The Ingress puts the frontend and the API on one host.** `/api` routes to `citizen-service`,
  `/` routes to `frontend`. This only works because the frontend image is built with
  `VITE_API_BASE_URL=""` (see below) — the browser then calls relative `/api/...` paths against
  whatever host served the page, landing on the same Ingress, same origin, no CORS to configure
  for the browser↔API path at all.
- **`citizen-uploads-pvc` is `ReadWriteOnce` with 2 replicas mounting it.** That only works
  because a single-node kind/minikube cluster schedules both pods onto the same node. It will
  break on a real multi-node cluster — see the comment in `citizen-service/deployment.yaml` for
  the fix (a `ReadWriteMany` StorageClass, or move uploads to object storage).

## Quick start (recommended)

The whole Phase 8 flow — create cluster, install ingress-nginx, build images, load them, apply
manifests, wait for rollout, print next steps — is automated:

```bash
# kind (recommended — faster, more predictable than minikube for this)
./scripts/deploy-kind.sh

# or minikube, if that's what you already have installed
./scripts/deploy-minikube.sh
```

Both scripts are idempotent — safe to re-run if something fails partway through. When you're
done:

```bash
./scripts/teardown.sh kind       # or: ./scripts/teardown.sh minikube
```

Once deployed, verify everything actually works end to end:

```bash
./scripts/smoke-test.sh
```

This registers a real citizen, logs in, browses the seeded services, submits a request, and
confirms it shows up — the same flow described in Phases.md's "Try it out", but scripted and
asserting at each step instead of eyeballing it in a browser.

## Manual steps (if you want to understand or customize the flow)

### Building images for a local cluster

kind and minikube don't pull from Docker Hub by default for locally-tagged images — build then
load them into the cluster's own image store.

```bash
cd digital-citizen-portal

docker build -t citizen-service:latest ./citizen-service
docker build -t notification-service:latest ./notification-service

# IMPORTANT: VITE_API_BASE_URL must be an empty string here — Vite bakes it into the JS bundle at
# build time, and this Ingress setup expects the frontend to call relative "/api/..." paths (see
# "Design decisions" above). This is NOT the same value used for docker-compose's frontend build,
# which points at http://localhost:8000 directly.
docker build -t frontend:latest \
  --build-arg VITE_API_BASE_URL="" \
  ./frontend
```

Then load into whichever local cluster you're using:

```bash
# kind
kind load docker-image citizen-service:latest
kind load docker-image notification-service:latest
kind load docker-image frontend:latest

# minikube
minikube image load citizen-service:latest
minikube image load notification-service:latest
minikube image load frontend:latest
```

## Ingress controller note (important)

This project uses **ingress-nginx** (`kubernetes/ingress-nginx`), pinned to `v1.15.1` in
`scripts/deploy-kind.sh`. The Kubernetes Steering and Security Response Committees
[announced in November 2025 and confirmed at retirement in March 2026](https://www.kubernetes.io/blog/2026/01/29/ingress-nginx-statement/)
that this project is now retired: no further releases, bugfixes, or security patches will be
published. It still installs and runs correctly for local kind/minikube development — which is
all this phase needs — but **this is not a safe choice for anything internet-facing or
production**. If this project ever moves past a local demo cluster, replace ingress-nginx with
an actively maintained alternative (Traefik, the F5 NGINX Ingress Controller, or a Gateway API
implementation like Envoy Gateway) before exposing it to real traffic. Noted here rather than
silently pinning an unmaintained default.

### Applying the manifests directly (skipping the deploy scripts)

```bash
kubectl apply -k k8s/
kubectl get pods -n citizen-portal -w
```

Add `citizen-portal.local` to your hosts file pointing at the cluster's ingress IP
(`127.0.0.1` for kind with the `extraPortMappings` in `k8s/kind-config.yaml`, or `minikube ip`
for minikube), then visit `http://citizen-portal.local`. This is exactly what
`scripts/deploy-kind.sh` / `scripts/deploy-minikube.sh` do for you automatically, in the right
order, with rollout waits — use the manual path only if you're debugging one step in isolation.

## Validating manifests without a cluster

No `kubectl` and no live cluster were available in the environment this phase was built in, so
these manifests were checked for internal consistency instead:

- Every YAML file parses cleanly (all 21 manifests, plus `kind-config.yaml` and
  `kustomization.yaml`)
- Every `ConfigMap`/`Secret`/`PVC` reference inside a `Deployment` resolves to an object that's
  actually declared somewhere in `k8s/`
- Every `Service` selector matches its corresponding `Deployment`'s pod labels
- Every namespaced object consistently sets `namespace: citizen-portal`
- `kustomization.yaml`'s `resources` list was checked against the filesystem — all 21 paths exist
- Every shell script in `scripts/` passes `bash -n` (syntax-only check — no execution)
- The ingress-nginx manifest URL used in `scripts/deploy-kind.sh` was confirmed to exist and
  point at the current last-maintained release (see the Ingress controller note above)

This is **not** a substitute for actually running `scripts/deploy-kind.sh` against a real
cluster — that check has not happened yet. Treat the deploy scripts as reviewed-but-untested
until you run them for the first time; if anything fails, that's expected on a first run of new
automation, not a sign something is fundamentally wrong.

## What's deferred to later phases

- Prometheus/Grafana/Loki/Alertmanager (ServiceMonitors, log shipping) — **Phase 9**
- Chaos engineering endpoints — **Phase 10**
- CI/CD building and pushing these images to a real registry, replacing `imagePullPolicy:
  IfNotPresent` + local image loading with a proper `image: registry/…:tag` + `imagePullSecrets`
  — **Phase 11**
- Real secret management (SOPS / External Secrets) replacing the plain `Secret` manifests here,
  which use the same demo-only placeholder credentials as `.env.example` — also **Phase 11**
- Migrating off ingress-nginx to an actively maintained ingress controller — see the note above;
  worth doing before Phase 9's observability stack adds any public-facing surface
- HorizontalPodAutoscalers — not planned yet, noted here as a possible improvement once there's
  real traffic data to size against
