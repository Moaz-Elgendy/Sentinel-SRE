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
├── ingress/
│   └── ingress.yaml            # one host, "/" -> frontend, "/api" -> citizen-service
└── monitoring/                 # Phase 9 — observability stack, none of it Ingress-exposed
    │                             # (see "Accessing the observability stack" below)
    ├── prometheus/
    │   ├── rbac.yaml            # ServiceAccount+Role+RoleBinding for pod-based scrape discovery
    │   ├── configmap.yaml       # scrape_configs — annotation-based discovery, see below
    │   ├── rules-configmap.yaml # first alerting rule set (ServiceDown, error rate, latency)
    │   ├── deployment.yaml
    │   └── service.yaml          # ClusterIP :9090
    ├── alertmanager/
    │   ├── configmap.yaml        # minimal routing tree, no receiver wired yet — see note below
    │   ├── deployment.yaml
    │   └── service.yaml          # ClusterIP :9093
    ├── loki/
    │   ├── configmap.yaml        # single-binary mode, filesystem storage
    │   ├── deployment.yaml
    │   └── service.yaml          # ClusterIP :3100
    ├── alloy/                    # NOT Promtail — see "Ingress controller note"-style
    │   │                          # callout below; Promtail is deprecated
    │   ├── rbac.yaml
    │   ├── configmap.yaml         # River config: discover pods, tail via k8s API, ship to Loki
    │   └── daemonset.yaml
    └── grafana/
        ├── secret.yaml                       # admin credentials (demo placeholder)
        ├── datasources-configmap.yaml         # provisions Prometheus + Loki, no manual setup
        ├── dashboard-provider-configmap.yaml
        ├── dashboard-json-configmap.yaml       # one starter dashboard
        ├── deployment.yaml
        └── service.yaml                        # ClusterIP :3000

scripts/
├── deploy-docker-desktop.sh    # full automated deploy to Docker Desktop's built-in Kubernetes
├── deploy-kind.sh              # full automated deploy to a local kind cluster
├── deploy-minikube.sh          # full automated deploy to a local minikube cluster
├── teardown.sh                 # tears down a kind or minikube cluster
└── smoke-test.sh                # end-to-end flow test against a live deployment
```

## Accessing the observability stack

None of Prometheus, Grafana, Loki, or Alertmanager are exposed through the Ingress — they're
internal tools you reach with `kubectl port-forward`, not citizen-facing:

```bash
kubectl port-forward -n citizen-portal svc/grafana 3001:3000
# open http://localhost:3001 — login admin / sentinal (see grafana/secret.yaml)
# "Citizen Portal Overview" dashboard is pre-provisioned, no setup needed

kubectl port-forward -n citizen-portal svc/prometheus 9090:9090
# open http://localhost:9090 — check Status > Targets to confirm both
# citizen-service and notification-service pods are being scraped

kubectl port-forward -n citizen-portal svc/alertmanager 9093:9093
# open http://localhost:9093 — see any currently-firing alerts
```

(Port 3001 for Grafana, not 3000 — the frontend's Service already uses 3000, and
`kubectl port-forward` binds to your local machine, not inside the cluster, so the two would
collide if you ran both port-forwards with the same local port at once.)

### How scraping works (no Prometheus Operator / ServiceMonitor CRDs yet)

`citizen-service` and `notification-service`'s pod templates carry
`prometheus.io/scrape: "true"`, `prometheus.io/port: "8000"`, `prometheus.io/path: "/metrics"`
annotations (see their `deployment.yaml` files). Prometheus's `kubernetes_sd_configs` (role:
`pod`, scoped to the `citizen-portal` namespace) discovers targets by matching on exactly those
annotations — see `monitoring/prometheus/configmap.yaml`'s `relabel_configs`. This is the
classic annotation-based pattern that predates the Prometheus Operator's `ServiceMonitor` CRD;
moving to the Operator is a reasonable future step once there's a reason to manage more than two
scrape targets, but it's unnecessary complexity for this project's current size.

### A note on Alloy vs. Promtail

This stack ships logs with **Grafana Alloy**, not Promtail. While researching which log shipper
to use, it turned out Promtail — the tool you'd expect by default alongside Loki — is now
deprecated; its functionality was merged into Alloy. Rather than build Phase 9 on a tool already
on its way out, `monitoring/alloy/` uses Alloy's River-syntax config from the start. It also
tails logs via the Kubernetes API (`loki.source.kubernetes`) rather than reading host log files,
which avoids the `hostPath` mounts and elevated privileges a classic Promtail DaemonSet needs —
a nice side benefit, not just a forced migration.

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

The whole Phase 8 flow — cluster/ingress setup, build images, apply manifests, wait for
rollout, print next steps — is automated. Pick the script matching your local cluster:

```bash
# Docker Desktop's built-in Kubernetes (no separate cluster tool needed —
# it's already running, and shares Docker Desktop's image store directly)
./scripts/deploy-docker-desktop.sh

# kind (a separate cluster-in-a-container, if you don't use Docker Desktop's own Kubernetes)
./scripts/deploy-kind.sh

# minikube, if that's what you already have installed instead
./scripts/deploy-minikube.sh
```

All three are idempotent — safe to re-run if something fails partway through. If you've already
built the images yourself (matching this project's plain `citizen-service:latest` /
`notification-service:latest` / `frontend:latest` naming — see "Image naming" below),
`deploy-docker-desktop.sh --skip-build` skips the rebuild step.

When you're done, for kind/minikube:

```bash
./scripts/teardown.sh kind       # or: ./scripts/teardown.sh minikube
```

(Docker Desktop's Kubernetes isn't a disposable cluster the same way — just `kubectl delete -k
k8s/` to remove the app's resources, or turn Kubernetes off in Docker Desktop's settings.)

Once deployed, verify everything actually works end to end:

```bash
./scripts/smoke-test.sh
```

This registers a real citizen, logs in, browses the seeded services, submits a request, and
confirms it shows up — the same flow described in Phases.md's "Try it out", but scripted and
asserting at each step instead of eyeballing it in a browser.

### Image naming

The Deployment manifests reference plain image names — `citizen-service:latest`,
`notification-service:latest`, `frontend:latest` — matching a straightforward local `docker
build -t <name>:latest .`, not a `citizen-portal/` namespace prefix. If you build with different
tags, update the `image:` field in each `k8s/*/deployment.yaml` to match (there are two
references in `citizen-service/deployment.yaml` and `notification-service/deployment.yaml` each
— one on the `initContainer`, one on the main container — both need to match).

### Which local cluster should I use?

- **Already have Docker Desktop with Kubernetes enabled?** Use `deploy-docker-desktop.sh`. It's
  simplest for this project specifically: the cluster is already running, and — unlike kind or
  minikube — it shares Docker Desktop's own image store directly, so a plain `docker build` is
  immediately visible to the cluster with no separate "load the image into the cluster" step at
  all. It also natively supports `LoadBalancer` Services bound to `localhost`, so ingress-nginx
  needs no NodePort/extraPortMappings workaround either.
- **Don't have Docker Desktop's Kubernetes, or want an isolated/disposable cluster?** Use `kind`
  — it's a full cluster running in a container, separate from anything else on your machine, and
  trivial to tear down and recreate.
- **Already have minikube set up?** `deploy-minikube.sh` works too — no strong reason to switch
  if it's already what you use.

## Manual steps (if you want to understand or customize the flow)

### Building images for a local cluster

**Docker Desktop's Kubernetes**: skip straight to the `docker build` commands below — no load
step needed, it shares Docker Desktop's image store directly.

**kind and minikube**: these don't pull from Docker Hub by default for locally-tagged images —
build, then load them into the cluster's own image store (commands further down).

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

## Troubleshooting

**Pods are `Running` but I don't see any containers in Docker Desktop's GUI.** Expected, not a
bug. Docker Desktop's Kubernetes runs pods through `containerd` via `kubelet` — a separate
runtime path from the Docker Engine that the GUI's "Containers" tab lists. Your pods are real;
they just won't show up there. Use `kubectl get pods -n citizen-portal` (or Docker Desktop's
separate "Kubernetes" view, if your version has one) instead of the Containers tab.

**The site loads on `localhost:3000` (via `kubectl port-forward svc/frontend 3000:3000`), but
API calls fail / port 8000 isn't reachable.** This isn't just a missing port-forward — it's a
consequence of how the frontend image is built. `VITE_API_BASE_URL=""` (see "Design decisions"
above) makes the frontend call *relative* paths like `/api/auth/login`, expecting an Ingress in
front of it to route `/api` → citizen-service and `/` → frontend on the same origin.
`kubectl port-forward`-ing the frontend alone gets you the page, but `/api/...` then resolves to
`localhost:3000/api/...` — and port 3000 has no idea what to do with that, because that routing
only exists in the Ingress. Two fixes:
1. **Recommended**: run one of the `deploy-*.sh` scripts (or just apply
   `k8s/ingress/ingress.yaml` if you've already got everything else running) and hit
   `http://citizen-portal.local` instead of port-forwarding the frontend directly. No
   port-forwarding needed at all once the Ingress is up.
2. **Quick workaround, not the intended setup**: also run `kubectl port-forward svc/citizen-service
   8000:8000 -n citizen-portal` in a second terminal, and rebuild the frontend image with
   `--build-arg VITE_API_BASE_URL="http://localhost:8000"` instead of `""`. Works, but requires
   two port-forwards running simultaneously and doesn't match how Phase 8 is designed to run.

**`citizen-portal.local` doesn't resolve.** Confirm the hosts-file entry is actually in place
(see "Quick start" above for the exact path per OS) and that you're pointing it at the right IP:
`127.0.0.1` for Docker Desktop or kind, `minikube ip`'s output for minikube. Check
`kubectl get svc -n ingress-nginx ingress-nginx-controller` — for Docker Desktop, `EXTERNAL-IP`
should read `localhost`.

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
- Chaos engineering endpoints — **Phase 10 (implemented)**

### Phase 10 chaos controls

Both backend Deployments now support controlled, authenticated failure injection. Set `CHAOS_MODE: "true"`
and replace the demo `CHAOS_ADMIN_TOKEN` in the service Secret before using it. The control endpoints are:

```text
GET  /api/chaos/status
POST /api/chaos/fault
POST /api/chaos/reset
```

Send the token in `X-Chaos-Token`. Citizen Service supports `latency_ms`, `error_rate`, and `db_failure`;
Notification Service additionally supports `notification_failure_rate`. Control state is in-memory per pod,
which makes pod-specific experiments possible with `kubectl port-forward`. Health, metrics, and control
paths are excluded from request fault injection. Phase 9 Prometheus/Grafana configuration now exposes and
visualizes the active chaos state and injected faults, so the observability pipeline can be tested directly
against Phase 10 incidents.
- CI/CD building and pushing these images to a real registry, replacing `imagePullPolicy:
  IfNotPresent` + local image loading with a proper `image: registry/…:tag` + `imagePullSecrets`
  — **Phase 11**
- Real secret management (SOPS / External Secrets) replacing the plain `Secret` manifests here,
  which use the same demo-only placeholder credentials as `.env.example` — also **Phase 11**
- Migrating off ingress-nginx to an actively maintained ingress controller — see the note above;
  worth doing before Phase 9's observability stack adds any public-facing surface
- HorizontalPodAutoscalers — not planned yet, noted here as a possible improvement once there's
  real traffic data to size against
