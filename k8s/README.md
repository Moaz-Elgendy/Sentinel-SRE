# Kubernetes manifests

Plain YAML manifests, organised as a Kustomize base with two overlays, that run the same stack
`docker compose up` brings up locally — on a local Kubernetes cluster (kind / minikube / Docker
Desktop) or on the single-node K3s cluster on AWS. No Helm — see `Phases.md` for why.

**`kubectl apply -k k8s/` is unchanged.** The manifests moved from `k8s/*` into `k8s/base/*` in
Phase 13, but the command, and all three `scripts/deploy-*.sh` that call it, behave exactly as they
did before. If you only ever run this locally, the only thing that changed for you is where the
files live.

**AWS status: nothing in `overlays/aws/` has ever been applied.** No pod has started on AWS, and
the Terraform in `infra/terraform/` has not been planned, let alone applied. The AWS path below
describes what the code does, not a run that happened. The local path, by contrast, has been run
for real since Phase 8.

## Layout

```text
k8s/
├── kustomization.yaml         # local entrypoint: `kubectl apply -k k8s/` -> overlays/local
├── kind-config.yaml           # kind cluster config (ingress-ready node, host port mappings)
│
├── base/                      # everything both environments share
│   ├── kustomization.yaml
│   ├── namespace.yaml
│   ├── postgres/              # citizen-service's Postgres
│   │   ├── pvc.yaml
│   │   ├── deployment.yaml
│   │   └── service.yaml
│   ├── notification-postgres/ # notification-service's own Postgres (one DB per microservice)
│   │   ├── pvc.yaml
│   │   ├── deployment.yaml
│   │   └── service.yaml
│   ├── citizen-service/
│   │   ├── configmap.yaml     # non-secret env (DB host/port/name, JWT alg, CORS origins, ...)
│   │   ├── pvc.yaml           # /data/uploads
│   │   ├── deployment.yaml    # initContainer runs migrations + seed once per rollout
│   │   └── service.yaml       # ClusterIP :8000
│   ├── notification-service/
│   │   ├── configmap.yaml
│   │   ├── deployment.yaml    # initContainer runs migrations
│   │   └── service.yaml       # ClusterIP :8000 — never exposed via Ingress, server-to-server only
│   ├── frontend/
│   │   ├── deployment.yaml
│   │   └── service.yaml       # ClusterIP :3000
│   ├── ingress/
│   │   └── ingress.yaml       # one host, "/" -> frontend, "/api" -> citizen-service
│   └── monitoring/            # Phase 9 — observability stack, none of it Ingress-exposed
│       │                      # (see "Accessing the observability stack" below)
│       ├── prometheus/
│       │   ├── rbac.yaml            # ServiceAccount+Role+RoleBinding for pod-based scrape discovery
│       │   ├── configmap.yaml       # scrape_configs — annotation-based discovery, see below
│       │   ├── rules-configmap.yaml # alerting rules (ServiceDown, error rate, latency, chaos)
│       │   ├── deployment.yaml
│       │   └── service.yaml         # ClusterIP :9090
│       ├── alertmanager/
│       │   ├── configmap.yaml       # minimal routing tree; the AWS overlay adds the Sentinel receiver
│       │   ├── deployment.yaml
│       │   └── service.yaml         # ClusterIP :9093
│       ├── loki/
│       │   ├── configmap.yaml       # single-binary mode, filesystem storage
│       │   ├── deployment.yaml
│       │   └── service.yaml         # ClusterIP :3100
│       ├── alloy/                   # NOT Promtail — see the note below; Promtail is deprecated
│       │   ├── rbac.yaml
│       │   ├── configmap.yaml       # River config: discover pods, tail via k8s API, ship to Loki
│       │   └── daemonset.yaml
│       └── grafana/
│           ├── datasources-configmap.yaml       # provisions Prometheus + Loki, no manual setup
│           ├── dashboard-provider-configmap.yaml
│           ├── dashboard-json-configmap.yaml    # one starter dashboard
│           ├── deployment.yaml
│           └── service.yaml                     # ClusterIP :3000
│
├── overlays/local/            # kind / minikube / Docker Desktop
│   ├── kustomization.yaml     # ../../base + the demo Secrets, nothing else
│   └── secrets/               # committed demo credentials — laptop-only, see below
│       ├── citizen-postgres-secret.yaml
│       ├── notification-postgres-secret.yaml
│       ├── citizen-service-secret.yaml
│       ├── notification-service-secret.yaml
│       └── grafana-secret.yaml
│
└── overlays/aws/              # single-node K3s on EC2
    ├── kustomization.yaml     # ../../base + ECR images, real Secrets, Sentinel
    ├── patch-configmaps.yaml        # chaos enabled, CORS pointed at the public IP
    ├── patch-ingress-traefik.yaml   # ingressClassName nginx -> traefik, host-less rule
    ├── patch-app-deployments.yaml   # ECR pull secret, revisionHistoryLimit, startup probes
    ├── patch-pvcs.yaml              # explicit local-path StorageClass
    ├── patch-replicas.yaml          # right-sized for a 2 vCPU node
    ├── patch-monitoring.yaml        # Alertmanager -> Sentinel webhook, extra alert rules
    ├── secrets/                     # *.env are gitignored; only *.env.example is committed
    └── sentinel/
        ├── namespace-rbac.yaml      # ServiceAccount + namespaced Role + RoleBinding
        ├── configmap.yaml           # allow-lists, confidence thresholds, bounds
        ├── deployment.yaml          # single replica, :8080
        └── service.yaml             # ClusterIP :8080

scripts/
├── deploy-docker-desktop.sh    # full automated deploy to Docker Desktop's built-in Kubernetes
├── deploy-kind.sh              # full automated deploy to a local kind cluster
├── deploy-minikube.sh          # full automated deploy to a local minikube cluster
├── deploy-aws.sh               # renders overlays/aws and applies it — runs ON the EC2 node
├── generate-aws-secrets.sh     # creates the gitignored overlays/aws/secrets/*.env files
├── teardown.sh                 # tears down a kind or minikube cluster
└── smoke-test.sh               # end-to-end flow test against a live deployment
```

## Why the manifests moved into `base/`

Kustomize refuses an overlay whose base is a parent directory that contains that overlay — it
reports a cycle and fails. With the manifests at the `k8s/` root, `k8s/overlays/aws` would have had
to declare `../..` as its base, which is exactly the case Kustomize rejects. There is no
configuration that makes it work: a real `base/` directory is required the moment a second
environment exists.

So the manifests moved, and `k8s/kustomization.yaml` became a thin pointer at `overlays/local` so
that the command everyone already types keeps working:

```text
local dev  ->  kubectl apply -k k8s/            (unchanged; wraps overlays/local)
AWS        ->  kubectl apply -k k8s/overlays/aws
```

The base **is** the local configuration: ingress-nginx, `:latest` images from a local image store.
The AWS overlay replaces exactly those things and adds Sentinel. Anything that should be true in
both environments belongs in `base/`, so the two cannot drift.

## Why the demo Secrets moved to `overlays/local/secrets/`

They were in `k8s/*/secret.yaml` before; they are now in `k8s/overlays/local/secrets/`. This is a
security decision rather than tidiness.

Those files contain literal committed passwords — `DATABASE_PASSWORD: sentinal`,
`CHAOS_ADMIN_TOKEN: replace_me`. That is a fine trade for a throwaway cluster on a laptop, and it
is what makes `kubectl apply -k k8s/` a single command with nothing to configure first. It is not a
fine trade on an internet-facing EC2 instance.

Keeping them in an overlay rather than in the base means **the AWS overlay does not inherit them at
all** — it cannot accidentally deploy a known password, because the manifest simply is not in its
resource list. The alternative (leave them in `base/`, override them in the AWS overlay) would work
right up until someone forgot the override, and would then fail silently, which is the worst
possible failure mode for a credential.

The AWS overlay generates its Secrets with a `secretGenerator` reading gitignored `.env` files from
`overlays/aws/secrets/`. Those files are absent from a fresh checkout, so `kubectl apply -k` fails
with a clear "no such file or directory" until they exist. That is deliberate: failing closed beats
defaulting to a published credential on a public host. `scripts/generate-aws-secrets.sh` creates
them once, and each has a committed `.env.example` documenting the fields.

## Quick start — local (recommended)

The whole flow — cluster/ingress setup, build images, apply manifests, wait for rollout, print next
steps — is automated. Pick the script matching your local cluster:

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

Applying the manifests directly, skipping the deploy scripts:

```bash
kubectl apply -k k8s/
kubectl get pods -n citizen-portal -w
```

Add `citizen-portal.local` to your hosts file pointing at the cluster's ingress IP (`127.0.0.1`
for Docker Desktop or kind with the `extraPortMappings` in `k8s/kind-config.yaml`, or `minikube ip`
for minikube), then visit `http://citizen-portal.local`. This is exactly what the `deploy-*.sh`
scripts do for you automatically, in the right order, with rollout waits — use the manual path only
if you're debugging one step in isolation.

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

## The AWS path

Read `docs/aws-deployment.md` first; this is the short version, and none of it has been run.

The cluster is single-node K3s on one EC2 instance, and **there is no SSH** — no port 22 rule and
no key pair. Everything below happens over AWS Systems Manager Session Manager, and the Kubernetes
API on `:6443` is not exposed publicly either.

```bash
# 1. Provision (from a laptop). Terraform state is local, no S3 backend.
cd infra/terraform && terraform init && terraform plan && terraform apply

# 2. Open a shell on the node. No SSH key involved.
aws ssm start-session --target <instance-id> --region <region>

# 3. Once, before the first deploy: create the gitignored secret env files.
sudo /opt/sentinel-sre/scripts/generate-aws-secrets.sh

# 4. Deploy a specific commit that CI has already pushed to ECR.
sudo /opt/sentinel-sre/scripts/deploy-aws.sh <git-sha>
```

Thereafter a push to `main` deploys itself: GitHub Actions runs the tests, builds and pushes to
ECR, then uses `aws ssm send-command` to invoke a fixed script on the node. CI never holds a
kubeconfig and never contacts the Kubernetes API.

`scripts/deploy-aws.sh` renders `overlays/aws` with `kubectl kustomize` and substitutes three
placeholders in the output stream rather than editing tracked files:

| Placeholder | Why it can't be resolved at build time |
|---|---|
| `ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com` | depends on the AWS account and region |
| `:PLACEHOLDER` (image tag) | depends on the commit being deployed |
| `PUBLIC_IP_PLACEHOLDER` | depends on the instance's IP |

All three are deliberately invalid rather than plausible defaults, so a substitution that silently
fails to happen produces an immediate, visible error (`ImagePullBackOff`, or a CORS rejection in
the browser) instead of quietly running the wrong thing.

### What the AWS overlay changes, and why each change is needed

- **Images → ECR, pinned by git SHA.** The base uses local `:latest` tags that only exist in a
  kind/minikube image store. Nothing is deployed by `latest` on AWS: the running ReplicaSet has to
  be traceable back to a commit, because that is what Sentinel's deployment correlation and
  rollback depend on.
- **Ingress → Traefik.** K3s bundles and installs Traefik, so the overlay switches
  `ingressClassName` from `nginx` to `traefik`. It also replaces the base's `host:
  citizen-portal.local` rule with a host-less one, because the AWS environment is reached by raw
  IP and a host-scoped rule would never match. This is also where the ingress-nginx retirement
  problem finally gets resolved for the environment that actually mattered (see the note below).
- **Pull secret → `ecr-credentials`**, refreshed on the node by a systemd timer, since ECR tokens
  expire.
- **Secrets → generated from gitignored env files**, as described above.
- **Storage → an explicit `local-path` StorageClass**, K3s's provisioner. PVCs become directories
  on the node's root EBS volume: durable across pod recreation and reboot, **not** durable across
  instance replacement.
- **`revisionHistoryLimit` set explicitly**, so Sentinel has rollback targets. Rollback is only
  possible if the previous ReplicaSet still exists.
- **Chaos enabled**, so the failure scenarios exist to demonstrate.
- **Sentinel added.** It has no local-development equivalent yet, so it is a resource in this
  overlay rather than a patch on the base. When a local Sentinel setup exists, it should be
  promoted into `base/`.
- **Replica counts right-sized for 2 vCPU.** Fixed by hand, no HPA.
- **Alertmanager gets a webhook receiver** pointing at `http://sentinel-ai:8080/api/alerts/webhook`,
  plus the additional alert rules the new chaos scenarios need (`HighCPUUsage`,
  `MemoryLeakSuspected`, `ChaosCPUBurn`, `ChaosMemoryLeak`, `NotificationDispatchFailures`) and two
  that watch Sentinel itself (`SentinelDown`, `SentinelEscalating`).

### Reaching the internal tools on AWS

Nothing except the portal on port 80 is publicly reachable. Grafana, Prometheus, Alertmanager and
Sentinel are all `ClusterIP`-only, reached with `kubectl port-forward` from a Session Manager shell
on the node — for example:

```bash
sudo kubectl -n citizen-portal port-forward svc/sentinel-ai 8080:8080
```

`docs/aws-deployment.md` has the full set, including how to tunnel a port through Session Manager
to a laptop.

## Accessing the observability stack (local)

None of Prometheus, Grafana, Loki, or Alertmanager are exposed through the Ingress — they're
internal tools you reach with `kubectl port-forward`, not citizen-facing:

```bash
kubectl port-forward -n citizen-portal svc/grafana 3001:3000
# open http://localhost:3001 — login admin / sentinal
# (see k8s/overlays/local/secrets/grafana-secret.yaml)
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

### How scraping works (no Prometheus Operator / ServiceMonitor CRDs)

`citizen-service` and `notification-service`'s pod templates carry
`prometheus.io/scrape: "true"`, `prometheus.io/port: "8000"`, `prometheus.io/path: "/metrics"`
annotations (see their `deployment.yaml` files in `base/`). Prometheus's `kubernetes_sd_configs`
(role: `pod`, scoped to the `citizen-portal` namespace) discovers targets by matching on exactly
those annotations — see `base/monitoring/prometheus/configmap.yaml`'s `relabel_configs`. This is
the classic annotation-based pattern that predates the Prometheus Operator's `ServiceMonitor` CRD;
moving to the Operator is a reasonable future step once there's a reason to manage more than a
handful of scrape targets, but it's unnecessary complexity for this project's current size.
Sentinel's own pod carries the same annotations on port 8080, so it is scraped the same way.

### A note on Alloy vs. Promtail

This stack ships logs with **Grafana Alloy**, not Promtail. While researching which log shipper
to use, it turned out Promtail — the tool you'd expect by default alongside Loki — is now
deprecated; its functionality was merged into Alloy. Rather than build Phase 9 on a tool already
on its way out, `base/monitoring/alloy/` uses Alloy's River-syntax config from the start. It also
tails logs via the Kubernetes API (`loki.source.kubernetes`) rather than reading host log files,
which avoids the `hostPath` mounts and elevated privileges a classic Promtail DaemonSet needs —
a nice side benefit, not just a forced migration.

## Design decisions worth knowing about

- **One Postgres Deployment per service, not a shared instance.** Matches the
  one-database-per-microservice boundary from Phase 2/5 — `citizen-postgres` and
  `notification-postgres` are fully independent, each with their own PVC and Secret.
- **Postgres stays in Kubernetes on AWS too, rather than moving to RDS.** Deliberate, and not
  only about cost: database failure is one of Sentinel's incident scenarios, and moving Postgres
  outside the cluster would put it outside the boundary Sentinel observes and delete the most
  interesting case in the project — the one where the correct autonomous action is *no action*.
- **`replicas: 1` and `strategy: Recreate` on both Postgres Deployments.** These are stateful,
  backed by a single `ReadWriteOnce` PVC. A `Deployment` here (rather than a `StatefulSet`) is
  the simplest thing that works for a single-instance demo database — don't scale these up.
- **Neither Postgres Deployment is on Sentinel's remediation allow-list**, and both are on a frozen
  deny-list that no environment variable can override. A database incident escalates to a human.
- **Migrations run in an `initContainer`, not the main container's start command.**
  `docker-compose.yml` runs `alembic upgrade head` as part of each container's boot command,
  which is fine at 1 replica. `citizen-service` and `notification-service` both run 2 replicas
  here — an `initContainer` runs the migration once per pod *before* it starts, and Alembic's own
  locking makes concurrent runs across the pods' init containers safe either way, but there's
  no reason to pay for a migration attempt on every pod boot when a clean initContainer pattern
  reads better and does the same job.
- **`notification-service` has no Ingress rule.** It's only ever called server-to-server by
  `citizen-service` (see `citizen-service/app/core/notifications.py`) — never directly by a
  browser — so it stays `ClusterIP`-only, same as both Postgres instances and the whole
  monitoring stack.
- **The Ingress puts the frontend and the API on one host.** `/api` routes to `citizen-service`,
  `/` routes to `frontend`. This only works because the frontend image is built with
  `VITE_API_BASE_URL=""` (see below) — the browser then calls relative `/api/...` paths against
  whatever host served the page, landing on the same Ingress, same origin, no CORS to configure
  for the browser↔API path at all.
- **`citizen-uploads-pvc` is `ReadWriteOnce` with 2 replicas mounting it.** That only works
  because a single-node cluster schedules both pods onto the same node — true for
  kind/minikube/Docker Desktop *and* for the single-node K3s cluster on AWS. It will break on a
  real multi-node cluster — see the comment in `base/citizen-service/deployment.yaml` for the fix
  (a `ReadWriteMany` StorageClass, or move uploads to object storage).
- **Sentinel gets a namespaced `Role`, never a `ClusterRole`.** The reasoning for every granted
  verb and every deliberate omission is written out in
  `overlays/aws/sentinel/namespace-rbac.yaml`; it is the single most important file in this
  directory to read before trusting an agent that acts on the cluster unattended.

### Image naming

The base Deployment manifests reference plain image names — `citizen-service:latest`,
`notification-service:latest`, `frontend:latest` — matching a straightforward local `docker
build -t <name>:latest .`, not a `citizen-portal/` namespace prefix. If you build with different
tags locally, update the `image:` field in each `k8s/base/*/deployment.yaml` to match (there are
two references in `citizen-service/deployment.yaml` and `notification-service/deployment.yaml`
each — one on the `initContainer`, one on the main container — both need to match).

The AWS overlay does not need this: its `images:` transformer rewrites those same names to ECR
repositories, which is exactly what the plain, unprefixed names in the base make easy.

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

## Chaos controls

Both backend Deployments support controlled, authenticated failure injection. Set
`CHAOS_MODE: "true"` and replace the demo `CHAOS_ADMIN_TOKEN` in the service Secret before using
it locally; the AWS overlay enables chaos by default, since demonstrating incidents is the point of
that environment. The control endpoints are:

```text
GET  /api/chaos/status
POST /api/chaos/fault
POST /api/chaos/reset
```

Send the token in `X-Chaos-Token`. A missing or wrong token returns **404, not 401** — the
existence of the control surface is not advertised to unauthenticated callers, so a 404 from a
chaos call means "bad token or chaos disabled", never "endpoint missing".

Both services support `latency_ms`, `error_rate`, `db_failure`, `cpu_burn` (bool) and
`memory_leak_mb` (int, 0–2048); Notification Service additionally supports
`notification_failure_rate`. Control state is in-memory per pod, which makes pod-specific
experiments possible with `kubectl port-forward`. Health, metrics, OpenAPI and the control paths
themselves are excluded from request fault injection, so an injected fault doesn't accidentally
make Kubernetes believe the pod is dead. Every fault field has a corresponding Prometheus gauge
(`chaos_latency_ms`, `chaos_error_rate`, `chaos_db_failure`, `chaos_cpu_burn`,
`chaos_memory_leak_mb`, `chaos_notification_failure_rate`, plus `chaos_injections_total`), which is
what lets Sentinel distinguish a deliberate fault from a real failure.

`scripts/incident-scenarios.sh` drives nine named scenarios through this API (`db-outage`,
`http-errors`, `latency`, `notification-degradation`, `full-outage`, `high-cpu`, `memory-leak`,
`crashloop`, `bad-deployment`) and asserts that the right Prometheus alert reaches `firing`.

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
   `k8s/base/ingress/ingress.yaml` if you've already got everything else running) and hit
   `http://citizen-portal.local` instead of port-forwarding the frontend directly. No
   port-forwarding needed at all once the Ingress is up.
2. **Quick workaround, not the intended setup**: also run `kubectl port-forward svc/citizen-service
   8000:8000 -n citizen-portal` in a second terminal, and rebuild the frontend image with
   `--build-arg VITE_API_BASE_URL="http://localhost:8000"` instead of `""`. Works, but requires
   two port-forwards running simultaneously and doesn't match how this is designed to run.

**`citizen-portal.local` doesn't resolve.** Confirm the hosts-file entry is actually in place and
that you're pointing it at the right IP: `127.0.0.1` for Docker Desktop or kind, `minikube ip`'s
output for minikube. Check `kubectl get svc -n ingress-nginx ingress-nginx-controller` — for
Docker Desktop, `EXTERNAL-IP` should read `localhost`. (This is a local-only problem: the AWS
overlay replaces the host-scoped Ingress rule with a host-less one, since that environment is
reached by IP.)

**`kubectl apply -k k8s/overlays/aws` fails with "no such file or directory" on a `.env` file.**
Working as intended — the AWS secret env files are gitignored and absent from a fresh checkout.
Run `scripts/generate-aws-secrets.sh` first. Failing closed here is deliberate; the alternative
would be defaulting to a committed password on a public host.

## Ingress controller note (important)

Local development uses **ingress-nginx** (`kubernetes/ingress-nginx`), pinned to `v1.15.1` in
`scripts/deploy-kind.sh`. The Kubernetes Steering and Security Response Committees
[announced in November 2025 and confirmed at retirement in March 2026](https://www.kubernetes.io/blog/2026/01/29/ingress-nginx-statement/)
that this project is now retired: no further releases, bugfixes, or security patches will be
published. It still installs and runs correctly for local kind/minikube development — which is all
the local path needs — but **it is not a safe choice for anything internet-facing**.

That finding was carried forward, unresolved, from Phase 8 through Phases 9, 10 and 11, and it is
resolved in Phase 13 for the environment where it actually mattered: **AWS uses Traefik**, which
K3s installs anyway, so nothing internet-facing in this project depends on an unmaintained
controller. Local development still uses ingress-nginx because the tradeoff there is genuinely
different — a laptop cluster with no inbound exposure — and swapping it would mean maintaining a
second local setup for no security gain.

## Validating manifests without a cluster

Since Phase 11 this is done in CI rather than by hand: `.github/workflows/ci-cd.yml`'s
`validate-k8s-manifests` job renders **both** overlays with `kubectl kustomize` and checks the
output against real Kubernetes API schemas with `kubeconform`, on top of the structural checks
earlier phases did manually (every `ConfigMap`/`Secret`/`PVC`/`ServiceAccount` reference resolves,
every `Service` selector matches its `Deployment`/`DaemonSet`'s pod labels, every namespaced object
sets `namespace: citizen-portal`). Every shell script in `scripts/` is checked with `bash -n`.

That is a real check and it is meaningfully stronger than what Phases 7–9 could do, but it is still
not a substitute for running the thing. The local path has been run against real clusters since
Phase 8. **The AWS overlay has not** — schema-valid manifests that render correctly are not the
same as pods that start. Treat `overlays/aws/` as reviewed-but-unrun; if something fails on its
first real apply, that's expected on a first run of new infrastructure, not a sign something is
fundamentally wrong.

## What's still deferred

- **Real secret management** (SOPS / External Secrets / AWS Secrets Manager) replacing both the
  committed demo Secrets in `overlays/local/` and the gitignored env files in `overlays/aws/`. The
  current split is a meaningful improvement over one set of committed credentials for both
  environments, but it still means the real values live in files an operator manages by hand.
- **TLS.** The AWS overlay serves plain HTTP on an IP. `:443` exists behind a Terraform variable
  that defaults to off, because nothing terminates TLS yet and a self-signed certificate on an IP
  address would be worse than plain HTTP.
- **A local Sentinel setup.** Sentinel is only in `overlays/aws/`, so the quickest cluster to bring
  up is the one where the agent can't be exercised.
- **`postgres_exporter` and a Postgres-specific alert**, without which "the database is actually
  down" (as opposed to "the application believes it is") is not something the stack can observe.
- **HorizontalPodAutoscalers.** Replica counts are fixed by hand in both environments; on AWS
  `MAX_REPLICAS=3` is what a 2 vCPU node can absorb, not a policy preference.
- **StatefulSets for Postgres**, and `ReadWriteMany` storage for uploads — both only matter on a
  multi-node cluster, which neither environment is.
