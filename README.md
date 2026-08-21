# Sentinel SRE

An autonomous SRE agent, and a realistic workload for it to watch.

This repository contains two things that only make sense together:

1. **The Digital Citizen Services Portal** — a small but genuinely multi-service application
   (React frontend, two FastAPI services, two PostgreSQL databases) built to behave like a real
   production system: structured logs with request correlation, Prometheus metrics, health probes
   that distinguish "up" from "healthy", alerting rules, and an authenticated API for injecting
   controlled failures.
2. **Sentinel AI** (`sentinel-ai/`) — an autonomous SRE agent that watches that application,
   investigates incidents, decides on remediation, and **executes it without asking a human**,
   including rolling back a bad deployment.

The application exists so Sentinel has something real to fail. Most demonstrations of "AI for
operations" are built on a fabricated metric and a fabricated failure; this one is built on an
application that can actually break, in several distinguishable ways, on demand.

> **The AWS environment in this repository is a demonstration and development environment. It is
> not a production government deployment architecture.** It is a single EC2 instance with no
> redundancy, no TLS, no database backups, and one availability zone. It is deliberately shaped
> to be cheap enough to leave running while Sentinel is developed against it. Nothing about it
> should be copied into a system with real users or real citizen data. The portal itself is a
> simulation — it does not represent any real government service, and every national ID or
> document in it is generated.
>
> **Additionally: the AWS environment has not yet been deployed.** The Terraform is written and
> validated but has never been applied. See [Limitations](#limitations).

---

## Contents

- [Problem statement](#problem-statement)
- [Solution](#solution)
- [Architecture](#architecture)
- [Technology stack](#technology-stack)
- [Microservices](#microservices)
- [AWS architecture](#aws-architecture)
- [K3s architecture](#k3s-architecture)
- [Observability architecture](#observability-architecture)
- [Sentinel architecture](#sentinel-architecture)
- [Autonomous remediation](#autonomous-remediation)
- [Rollback capability](#rollback-capability)
- [The incident lifecycle](#the-incident-lifecycle)
- [A worked example incident](#a-worked-example-incident)
- [Chaos engineering scenarios](#chaos-engineering-scenarios)
- [CI/CD](#cicd)
- [Security model](#security-model)
- [Deployment](#deployment) — [local](#local-development) and [AWS](#aws-deployment)
- [Cost-conscious AWS architecture](#cost-conscious-aws-architecture)
- [Limitations](#limitations)
- [Future roadmap](#future-roadmap)
- [Documentation map](#documentation-map)

---

## Problem statement

When a production system breaks, the work is rarely the fix. The work is everything around it:
reading the alert, pulling the metric that the alert summarised into a boolean, finding the log
lines for the affected requests, checking whether anything was deployed recently, forming a
hypothesis, deciding whether the safest action is a restart or a rollback or nothing at all,
executing it, confirming the system actually recovered rather than merely stopped alerting, and
then writing it all down.

Most of that is mechanical. It is also slow, and it is slowest exactly when it matters most —
at 3am, under pressure, by whoever happened to be on call. Monitoring platforms have got very good
at the first step and have deliberately stopped there: they tell a human that something is wrong
and leave every decision to that human.

The gap is not detection. The gap is the twenty minutes between detection and the correct action.

## Solution

Sentinel closes that gap for a bounded set of failures, by doing the mechanical parts
deterministically and using a language model only for the part that genuinely benefits from one:
reading heterogeneous evidence and proposing an explanation.

The design commitment that makes this defensible is that **the language model never touches the
cluster**. It receives evidence and returns a structured recommendation — an action name from a
fixed enum, a target, a confidence number, and its reasoning. Everything after that is ordinary
code: a Decision Engine that orders candidate actions, a Policy Engine that checks each against
allow-lists, confidence thresholds and preconditions, a Remediation Engine that is the only
component permitted to call the Kubernetes API, and a Kubernetes RBAC Role that would refuse
anything else even if both of those had bugs.

Within that boundary, Sentinel is genuinely autonomous. It restarts deployments, scales them
within bounds, clears chaos faults, and rolls back bad deployments, with no approval step. Outside
that boundary — a database incident, an unfamiliar failure mode, an action whose preconditions do
not hold — it escalates to a human and says exactly why.

## Architecture

```text
  Developer push
        │
        ▼
  GitHub Actions ──── OIDC ────▶ AWS
   tests → build → push to ECR → aws ssm send-command
                                        │
                                        ▼  (CI never touches the Kubernetes API)
  ┌─────────────────────────────────────────────────────────────────────────┐
  │ AWS: VPC · public subnet · Internet Gateway · security group (:80 only) │
  │                                                                         │
  │   one t3a.large EC2 instance — Ubuntu 24.04, 40 GiB encrypted gp3       │
  │   ┌───────────────────────────────────────────────────────────────────┐ │
  │   │ K3s v1.31.4+k3s1 — single node, control-plane + worker            │ │
  │   │                                                                   │ │
  │   │   Traefik (K3s's bundled ingress controller)                      │ │
  │   │     │                                                             │ │
  │   │     ├── / ────▶ frontend (nginx + React)                          │ │
  │   │     └── /api ─▶ citizen-service ──▶ notification-service          │ │
  │   │                       │                      │                    │ │
  │   │                 citizen-postgres    notification-postgres         │ │
  │   │                                                                   │ │
  │   │   Prometheus ── Alertmanager                Grafana Alloy         │ │
  │   │        │             │                            │               │ │
  │   │        │             │  webhook                   ▼               │ │
  │   │        │             └────────────┐             Loki              │ │
  │   │        │                          ▼               │               │ │
  │   │        └──── PromQL ──────▶  Sentinel AI  ◀── LogQL┘              │ │
  │   │                             (FastAPI :8080)                       │ │
  │   │                                   │                               │ │
  │   │                       namespaced RBAC Role                        │ │
  │   │                                   ▼                               │ │
  │   │                          Kubernetes API (:6443, not public)       │ │
  │   │                                                                   │ │
  │   │   Grafana ──── reads ────▶ Prometheus + Loki                      │ │
  │   └───────────────────────────────────────────────────────────────────┘ │
  └─────────────────────────────────────────────────────────────────────────┘
        │
        ▼
  GitHub issues / incident reports / postmortems · Slack (both optional)
```

Administration of the instance is AWS Systems Manager Session Manager only — there is no SSH port
and no key pair. Grafana, Prometheus, Alertmanager and Sentinel are not exposed publicly; they are
reached by port-forwarding through Session Manager. Port 80 is the only inbound rule.

## Technology stack

| Layer | Choice | Note |
|---|---|---|
| Frontend | React + Vite, served by nginx | Built with `VITE_API_BASE_URL=""` so the browser calls relative `/api/...` paths and there is no CORS to configure for the browser↔API path |
| Backend | Python, FastAPI, SQLAlchemy, Alembic | Two services, one database each |
| Database | PostgreSQL | In-cluster, one instance per service, not RDS |
| Containers | Docker | Multi-stage builds, non-root users |
| Orchestration | Kubernetes — K3s on AWS, kind / minikube / Docker Desktop locally | Kustomize base + two overlays |
| Ingress | Traefik on AWS (K3s's bundled controller), ingress-nginx locally | See [K3s architecture](#k3s-architecture) for why they differ |
| Metrics | Prometheus | Annotation-based pod discovery, no Operator/CRDs |
| Logs | Loki + Grafana Alloy | Alloy, not Promtail — Promtail is deprecated |
| Dashboards | Grafana | Datasources and one dashboard provisioned from ConfigMaps |
| Alerting | Alertmanager | Webhook receiver pointing at Sentinel |
| Agent | Python, FastAPI, `httpx`, SQLite | Optional OpenAI API for root-cause narrative |
| Infrastructure | Terraform (AWS provider 5.x) | Local state |
| CI/CD | GitHub Actions, ECR, OIDC, SSM | No long-lived AWS keys |

## Microservices

- **`frontend/`** — React SPA: registration, login, service catalogue, request submission, request
  status. Never calls `notification-service`; it only reflects state `citizen-service` exposes.
- **`citizen-service/`** (`:8000`) — the public API. Citizen registration and JWT auth, the service
  catalogue, request submission, document upload, request status. Owns the `citizen_portal`
  database. Hosts the chaos control API.
- **`notification-service/`** (`:8000` in-cluster) — notification dispatch with a simulated
  email/SMS provider. Called server-to-server by `citizen-service` with an ID-only payload
  (`citizen_id`, `request_id`) and never a database reference. Owns the `notification_service`
  database. It has no ingress rule at all; it is `ClusterIP`-only.
- **`sentinel-ai/`** (`:8080`) — the SRE agent. Receives Alertmanager webhooks at
  `/api/alerts/webhook`, reads Prometheus and Loki, reads and patches Deployments through the
  Kubernetes API.

Each backend owns its own database and its own migrations. There are no cross-service joins and no
shared schema — which is what makes "notification delivery is degraded but the portal is fine" a
state the system can actually be in, and therefore a state Sentinel has to distinguish.

## AWS architecture

Everything runs on one instance. The full list of AWS services used:

**VPC, Internet Gateway, one public subnet, one route table, one security group, EC2, EBS, IAM,
ECR, Systems Manager.**

Deliberately not used: **EKS, RDS, ALB, NAT Gateway**, and (on AWS) **ingress-nginx**. The
reasoning for each is in [Cost-conscious AWS architecture](#cost-conscious-aws-architecture) and
in full in [`docs/aws-deployment.md`](docs/aws-deployment.md).

The instance is a **`t3a.large`** (2 vCPU / 8 GiB) on Canonical's official **Ubuntu 24.04** AMI,
with a **40 GiB encrypted gp3** root volume. It has an IAM instance profile for SSM and ECR pulls,
and no other AWS permissions.

Four **ECR** repositories hold the images: `sentinel-sre-demo/citizen-service`,
`.../notification-service`, `.../frontend`, `.../sentinel-ai`. Deployments reference images **by
git commit SHA, never `latest`** — the running ReplicaSet has to be traceable back to a commit,
because "was something deployed just before this broke" is a question Sentinel answers
mechanically rather than by asking someone.

Infrastructure is Terraform in [`infra/terraform/`](infra/terraform/). State is local; there is no
S3 backend.

## K3s architecture

**K3s `v1.31.4+k3s1`, single node, control-plane and worker on the same instance.** K3s is a
conformant Kubernetes distribution in a single binary, and the API it exposes is the API the
manifests, `kubectl`, and Sentinel's Kubernetes client all target — so nothing in this repo is
K3s-specific except which ingress controller is installed.

The ingress controller **is** K3s-specific, in a useful way: K3s bundles **Traefik** and installs
it by default. The AWS overlay therefore uses Traefik, which also finally resolves a problem this
project has carried since Phase 8 — local development uses **ingress-nginx**, which
[was retired by the Kubernetes project](https://www.kubernetes.io/blog/2026/01/29/ingress-nginx-statement/)
and receives no further security patches. That is acceptable on a laptop cluster and is not
acceptable on anything reachable from the internet, so the two environments differ here on purpose.

Storage is K3s's **`local-path` provisioner** on the root EBS volume: a PVC becomes a directory on
the node's disk. This survives pod recreation and instance reboot. It does **not** survive instance
replacement — a `terraform destroy`/`apply` cycle starts from an empty database. The honest
description is "durable across the failures Sentinel is meant to handle, not the ones it is not".

Manifests live in a Kustomize base with two overlays:

```text
k8s/base/            shared manifests, configured for local development
k8s/overlays/local/  base + the committed demo Secrets  (kind / minikube / Docker Desktop)
k8s/overlays/aws/    base + Traefik, ECR images by SHA, real Secrets, Sentinel
```

`kubectl apply -k k8s/` still works exactly as it always did for local development. Details, and
why the manifests had to move into `base/`, are in [`k8s/README.md`](k8s/README.md).

## Observability architecture

Sentinel is only as good as what it can see, so the observability stack is a dependency of the
agent rather than a dashboard for humans:

- **Prometheus** scrapes both services via pod annotations (`prometheus.io/scrape`,
  `prometheus.io/port`, `prometheus.io/path`) — the classic annotation-based pattern, no Prometheus
  Operator and no `ServiceMonitor` CRDs for two scrape targets.
- **Grafana Alloy** tails pod logs through the Kubernetes API and ships them to **Loki**. Alloy
  rather than Promtail because Promtail is deprecated; reading through the API rather than host log
  files also avoids `hostPath` mounts and the privileges a classic Promtail DaemonSet needs.
- **Loki** stores structured JSON logs. Every log line carries a `request_id`, which is generated
  in `citizen-service`'s middleware and propagated to `notification-service`. That is the thread
  that turns "the 5xx rate is up" into "these specific requests failed, for this reason".
- **Alertmanager** receives Prometheus alerts and `POST`s them to Sentinel's webhook. This is a
  push signal: Sentinel does not poll for trouble, it is told.
- **Grafana** provisions its datasources and one starter dashboard from ConfigMaps, so there is no
  manual setup step.
- **Health probes distinguish liveness from readiness meaningfully.** `/readyz` returns HTTP 200
  with `status: "degraded"` when a downstream dependency is broken but the process is fine. This
  matters more than it sounds: it is why Sentinel's recovery validation parses the JSON body rather
  than trusting the status code.

## Sentinel architecture

```text
  Alertmanager webhook
        │
        ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  Evidence gathering (read-only)                                  │
  │    Prometheus (PromQL) · Loki (LogQL) · Kubernetes pods/events   │
  │    /readyz JSON · chaos_* gauges · Deployment + ReplicaSet history│
  └──────────────────────────────────────────────────────────────────┘
        │
        ▼
   ┌─────────┐   structured    ┌──────────┐   ordered    ┌────────┐
   │   LLM   │ ─ recommendation▶│ Decision │ ─candidates─▶│ Policy │
   │(optional)│   only          │  Engine  │              │ Engine │
   └─────────┘                 └──────────┘              └────────┘
                                                              │ permitted?
                                                              ▼
                                                      ┌─────────────┐
                                                      │ Remediation │
                                                      │   Engine    │
                                                      └─────────────┘
                                                              │ structured params
                                                              ▼
                                                    Kubernetes API (RBAC Role)
```

The layering is the whole security argument, so it is worth being precise about what each layer
does and does not do.

**The LLM analyses evidence and returns a structured recommendation.** An action name, a target, a
confidence score, and reasoning. It never receives shell access. It never receives `kubectl`. It
cannot name a target outside the allow-list, because the action name is parsed against a fixed enum
and the target is checked against a configured list before anything downstream runs — a returned
string like `restart_deployment; rm -rf /` does not parse to a valid action and is rejected. The
LLM is also **optional**: with no API key, Sentinel runs fully rule-based, and records
`llm_used=false` on the incident so that reading the record later is honest about what produced the
narrative.

**The Decision Engine turns a hypothesis into an ordered list of candidate actions.** It decides
what to try and in what order; it does not decide what is permitted. Having the ordering computed
here is what makes "the next safe action after a failed remediation" a well-defined thing rather
than an improvisation.

**The Policy Engine is the gate.** Allow-listed namespace, allow-listed deployment, action-specific
confidence threshold, per-incident action cap, per-target cooldown, replica bounds, and — for
rollback — a set of preconditions described below. It also enforces a **frozen deny-list**:
`citizen-postgres` and `notification-postgres` can never be remediated, and no environment
variable can change that.

**The Remediation Engine is the only component that calls the Kubernetes API**, and it accepts
structured parameters (namespace, deployment, target revision) — never a command string.

**The RBAC Role is the last line, and it matters precisely because the three layers above it are
ordinary application code that could contain a mistake.** See [Security model](#security-model).

Incident history is kept in SQLite on a node-local volume, which feeds the LEARNING phase and is
also why Sentinel runs as a single replica (see [Limitations](#limitations)).

## Autonomous remediation

Four actions exist. There is no fifth, and adding one means adding an enum member, a Policy Engine
branch, and possibly an RBAC verb — deliberately not a configuration change.

| Action | Confidence threshold | What it does |
|---|---|---|
| `restart_deployment` | ≥ 0.90 | Patches the pod template annotation — what `kubectl rollout restart` does. No pod deletion, so no `delete` verb is needed anywhere. |
| `rollback_deployment` | ≥ 0.95 | Patches the pod template back to a previous ReplicaSet's template. |
| `scale_deployment` | ≥ 0.90 | Patches replicas, within `MIN_REPLICAS=1` and `MAX_REPLICAS=3`. Never 0 — scaling to zero is an outage, not a fix. |
| `reset_chaos_fault` | ≥ 0.90 | `POST /api/chaos/reset` against the affected service, when the diagnosis is that the "incident" is a deliberately injected fault. |

Rollback carries the highest bar because it is the action with the largest effect: it changes what
code is running. The others are cheap and self-correcting by comparison.

A confidence number produced partly by a language model is a heuristic, and treating it as a
safety mechanism on its own would be a mistake. The parts that actually make an action safe are
the allow-list, the bounds, and the preconditions — the threshold just stops Sentinel acting on a
weak hypothesis.

Recovery validation runs after **every** action, and an incident is only marked RESOLVED once it
passes. It checks deployment availability, pod readiness, HTTP health (parsing the JSON `status`
field, because `/readyz` returns 200 with `status: "degraded"` when a dependency is broken — a
validator reading only the status code would declare a still-broken service recovered), the 5xx
error rate, p95 latency, CPU, memory, and the `chaos_*` gauges.

If validation fails, Sentinel re-investigates, takes the next candidate off the list, executes it,
and re-validates. When the candidates are exhausted or the per-incident action cap is reached, it
escalates. An agent that keeps inventing new things to try against a service that is not
recovering is strictly worse than one that stops and pages a human.

## Rollback capability

**Rollback requires no human approval.** Sentinel decides that a deployment caused an incident and
rolls it back, unattended. This is the intended behaviour and the reason the project exists; it is
not a mode that has to be enabled.

What makes that bounded rather than reckless is a set of conditions that all have to hold, none of
which the language model can influence:

- The target deployment is on the allow-list (`citizen-service`, `notification-service`,
  `frontend`) and not on the frozen deny-list.
- The namespace is on the allow-list (`citizen-portal`).
- Confidence is at least 0.95.
- A **previous revision actually exists** to roll back to.
- **Deployment history exists** — the ReplicaSets carrying the revision annotations are present.
  This is why the AWS overlay sets `revisionHistoryLimit` explicitly and why CI deploys by
  `kubectl set image`, which produces a clean new ReplicaSet.
- **A recent deployment correlates with the incident onset**, within a configured window
  (30 minutes by default). Without this, every incident gets blamed on the last deploy.
- **The rollback is reversible** — rolling forward again is possible.
- **Recovery validation is available for the target.** If Sentinel cannot verify that the rollback
  worked, it does not perform it. A target with no HTTP surface to probe is treated as
  "validation unavailable", and rollback is refused.

If any of these fails, the Policy Engine rejects the action and the Decision Engine's next
candidate is tried — or, if none remains, the incident escalates. The RBAC Role independently
bounds the damage: even a bug in all of the above cannot let Sentinel roll back something outside
its namespace, delete anything, or exec into a container.

`DRY_RUN=true` runs the entire lifecycle — detect, investigate, correlate, decide, policy-check,
validate — and logs the action it *would* have taken without touching the cluster. That is the
honest way to demonstrate Sentinel to someone not yet comfortable with it acting unattended, and
it is not the default.

## The incident lifecycle

```text
DETECTION  →  INVESTIGATION  →  CORRELATION  →  ROOT CAUSE ANALYSIS
           →  REMEDIATION DECISION  →  POLICY CHECK  →  AUTONOMOUS EXECUTION
           →  RECOVERY VALIDATION  →  DOCUMENTATION  →  NOTIFICATION  →  LEARNING
```

1. **DETECTION** — an Alertmanager webhook opens an incident. A repeat firing within the dedup
   window joins the existing incident rather than opening a second one, so Alertmanager's
   `repeat_interval` does not produce a storm.
2. **INVESTIGATION** — PromQL for the actual shape of the metric (not just the alert's boolean),
   LogQL for `request_id`-correlated log lines in the same window, Kubernetes pod status and
   Events for the things metrics never show: `CrashLoopBackOff`, `OOMKilled`, `FailedScheduling`,
   image pull errors.
3. **CORRELATION** — tie the signals to each other and to recent Deployment revisions. This is
   where "a deploy landed four minutes before the error rate moved" becomes a fact rather than a
   hunch.
4. **ROOT CAUSE ANALYSIS** — a hypothesis with a confidence score. Rule-based, optionally enriched
   by the LLM.
5. **REMEDIATION DECISION** — an ordered list of candidate actions derived from the root cause.
6. **POLICY CHECK** — allow-lists, thresholds, bounds, caps, cooldowns, preconditions.
7. **AUTONOMOUS EXECUTION** — the Remediation Engine performs the action and emits a Kubernetes
   Event, so the action is visible in `kubectl get events` alongside everything else that happened.
8. **RECOVERY VALIDATION** — settle, then poll the full check set until it passes or times out.
9. **DOCUMENTATION** — a structured incident record: timeline, evidence, hypothesis, actions taken,
   validation result. Optionally a GitHub issue and a postmortem.
10. **NOTIFICATION** — Slack and/or GitHub, both optional and both no-ops when unconfigured.
11. **LEARNING** — the incident is written to the store so patterns across incidents are visible,
    not just the most recent one.

On a failed remediation the loop re-enters at INVESTIGATION. On exhaustion it escalates.

## A worked example incident

The `bad-deployment` scenario, end to end. This is the case the rollback path exists for.

**t+0 — a healthy baseline.** `citizen-service` v1 is running. Error rate is near zero, p95 latency
is well inside its threshold, `/readyz` reports `status: "ok"`.

**t+1m — v2 is deployed.** CI pushes a new image by commit SHA and `sentinel-deploy.sh` sets it on
the Deployment. Kubernetes creates a new ReplicaSet; the old one stays, holding v1's exact pod
template — and therefore v1's exact image and git SHA.

**t+3m — errors rise.** The new revision returns 5xx on a significant fraction of requests. The
`http_requests_total{status=~"5.."}` rate climbs. `/readyz` still returns 200, because the process
is alive and its database is reachable — nothing here is a liveness failure, which is exactly why
Kubernetes will not fix this on its own.

**t+5m — DETECTION.** `HighHTTPErrorRate` reaches `firing` after its `for:` window and Alertmanager
`POST`s to Sentinel's webhook. Sentinel opens an incident.

**INVESTIGATION.** Sentinel pulls the 5xx rate and latency series over the alert window, pulls
`ERROR`-level log lines for `citizen-service` from Loki for the same window, reads pod status and
recent Events, and reads the `chaos_*` gauges.

**CORRELATION.** Two things line up. First, the `chaos_*` gauges are all at their resting values —
this is not a deliberately injected fault, so `reset_chaos_fault` is not the answer. Second, the
Deployment's ReplicaSet history shows a new revision created roughly two minutes before the error
rate moved, inside the 30-minute correlation window. The onset follows the deploy.

**ROOT CAUSE ANALYSIS.** Hypothesis: the current revision is faulty. Not a resource problem — CPU
and memory are normal. Not a dependency problem — the database is reachable and
`notification-service` is healthy. Confidence is high, because the correlation is a
strong signal and the alternatives have been actively ruled out rather than merely not considered.

**REMEDIATION DECISION.** The Decision Engine's ordered candidates put `rollback_deployment` first.
A restart would not help: the new code would simply start again and fail again. Scaling would not
help either — more replicas of broken code is more broken.

**POLICY CHECK.** `citizen-service` is allow-listed and not on the frozen deny-list.
`citizen-portal` is allow-listed. Confidence clears 0.95. A previous revision exists. Deployment
history exists. A recent deploy correlates. The rollback is reversible. Recovery validation is
available, because `citizen-service` has an HTTP surface to probe. Every precondition holds, so the
action is permitted.

**AUTONOMOUS EXECUTION.** Sentinel patches the Deployment's pod template back to the previous
ReplicaSet's template. No human is asked. A Kubernetes Event is emitted recording what it did and
why.

**RECOVERY VALIDATION.** Sentinel waits for the rollout to settle, then polls: the Deployment
reports its full replica count available, pods are ready, `/readyz` returns `status: "ok"` (parsed
from the body, not inferred from the 200), the 5xx rate is back under threshold, p95 latency is
under threshold, CPU and memory are normal, and the `chaos_*` gauges are still at rest. Validation
passes. The incident moves to RESOLVED.

**DOCUMENTATION and NOTIFICATION.** A structured incident record is written with the timeline, the
evidence, the hypothesis, the action, and the validation result — and, if configured, a GitHub
issue and a Slack message. Sentinel may *propose* a fix in the issue. It does not modify
application code and does not merge anything.

**LEARNING.** The incident is persisted, so a second bad deployment on the same service is a
recognised pattern rather than a fresh mystery.

**And the counterexample worth stating.** Had this been a `db-outage` instead, correlation would
have pointed at `citizen-postgres` — which is on the frozen deny-list. Sentinel would have
escalated to a human with its full diagnosis attached and taken no action at all. That is the
correct answer, not a gap in coverage.

## Chaos engineering scenarios

Failure is injected through an authenticated control API on the backend services, and driven by
[`scripts/incident-scenarios.sh`](scripts/incident-scenarios.sh):

```text
GET  /api/chaos/status    inspect current fault state
POST /api/chaos/fault     configure faults
POST /api/chaos/reset     clear all faults
```

Chaos is off unless `CHAOS_MODE=true`, every request needs an `X-Chaos-Token` header, and an
unauthenticated caller gets **404, not 401** — the existence of the control surface is not
advertised. Health, metrics, OpenAPI and the control endpoints themselves are excluded from fault
injection, so an injected fault does not accidentally make Kubernetes believe the pod is dead.

Fault fields on both backend services: `latency_ms`, `error_rate`, `db_failure`, `cpu_burn` (bool)
and `memory_leak_mb` (int, 0–2048), plus `notification_failure_rate` on `notification-service`.
All of them are exported as Prometheus gauges (`chaos_latency_ms`, `chaos_error_rate`,
`chaos_db_failure`, `chaos_cpu_burn`, `chaos_memory_leak_mb`, `chaos_notification_failure_rate`,
and `chaos_injections_total`), which is what lets Sentinel tell a deliberate fault from a real one.

Nine named scenarios:

| Scenario | What it exercises |
|---|---|
| `db-outage` | Database failure — and Sentinel's escalation path, since Postgres is not remediable |
| `http-errors` | Forced 5xx, the classic error-rate incident |
| `latency` | Injected latency with generated traffic (a histogram with no requests produces silence, not an alert) |
| `notification-degradation` | Partial degradation: the portal works, delivery does not |
| `full-outage` | `kubectl scale --replicas=0` — a real outage, not an injected one |
| `high-cpu` | CPU exhaustion, where a restart is genuinely the right first move |
| `memory-leak` | Growing memory, the slow-burn case |
| `crashloop` | `CrashLoopBackOff`, where a restart is *not* the answer |
| `bad-deployment` | A faulty revision — the rollback case, walked through above |

Each scenario pins the target to one replica first (chaos state is per-pod and in-memory),
generates real traffic where the alert needs it, and always resets the fault it injected — even if
interrupted.

## CI/CD

One workflow, [`.github/workflows/ci-cd.yml`](.github/workflows/ci-cd.yml):

```text
test-citizen-service ┐
test-notification-service ├─▶ build-and-push-images ─▶ deploy-to-k3s
test-sentinel-ai      │        (ECR, tagged by SHA)      (aws ssm send-command)
lint-and-build-frontend │
validate-k8s-manifests ┘
```

- **Tests** — the pytest suites for both application services and for Sentinel, plus frontend lint
  and build.
- **Manifest validation** — structural consistency checks *and* a real `kubeconform` schema check
  of `kubectl kustomize` output against genuine Kubernetes API schemas. **Both overlays** are
  validated, not just the local one.
- **Build and push** — only on `main`, only after everything above passes. Images go to ECR tagged
  with the git commit SHA.
- **Deploy** — `aws ssm send-command` invoking `/usr/local/bin/sentinel-deploy.sh images <sha>`,
  a fixed script installed on the node at provisioning time.

Two things about that deploy step are the point of it:

**Authentication is GitHub OIDC federation.** There are no long-lived AWS access keys anywhere.
The trust policy in [`infra/terraform/github_oidc.tf`](infra/terraform/github_oidc.tf) is scoped to
this repository and branch, and the role can reach the ECR repositories and send commands to one
instance — nothing else.

**CI never connects to the Kubernetes API.** It cannot: the API server is not reachable from the
internet. CI can only ask SSM to run one specific script with an image tag. There is no kubeconfig
in CI, no cluster credential to leak, and no way for a compromised workflow to run an arbitrary
command on the node.

## Security model

**Network.** Port 80 is the only inbound rule (443 exists behind a Terraform variable that defaults
to off, since nothing terminates TLS yet). There is **no port 22 rule and no EC2 key pair** —
administration is Systems Manager Session Manager, which works through the SSM agent's outbound
connection rather than an inbound listener. The Kubernetes API on `:6443` is not exposed publicly.
Prometheus, Grafana, Loki, Alertmanager and Sentinel are all `ClusterIP`-only and reached by
port-forwarding through Session Manager.

**Secrets.** The committed demo Secrets (with literal passwords like `sentinal`) live in
`k8s/overlays/local/secrets/` and are **not inherited by the AWS overlay** — that overlay generates
its Secrets from gitignored `.env` files instead. This is structural rather than procedural: the
AWS overlay cannot ship a published password by someone forgetting an override, because the
manifest is not in its resource list. A fresh checkout fails `kubectl apply -k` with a
missing-file error until the env files exist. Failing closed beats defaulting to a known credential
on a public host.

**Sentinel's Kubernetes RBAC** ([`k8s/overlays/aws/sentinel/namespace-rbac.yaml`](k8s/overlays/aws/sentinel/namespace-rbac.yaml))
is a dedicated ServiceAccount with a **namespaced `Role`, not `cluster-admin` and not a
`ClusterRole`**:

*Reads*: pods, services, endpoints, configmaps, events, deployments, replicasets, pod logs.
*Writes*: `patch`/`update` on `deployments` and `deployments/scale`, and `create` on Events.

What it explicitly **cannot** do, each omission deliberate:

- **No `pods/exec`, `pods/attach`, `pods/portforward`** — no arbitrary command execution inside a
  container. This is what makes "the LLM never gets shell access" a structural fact rather than a
  coding convention.
- **No `delete` on anything.** Restarts patch the pod template, so no delete verb is needed at all
   — which means Sentinel cannot delete a pod, a PVC, a Secret, or a namespace even by accident.
- **No access to Secrets, any verb.** Sentinel has no reason to read application credentials, and
  not being able to means a confused or compromised agent cannot leak them into an incident report,
  a GitHub issue, or an LLM prompt. That last one is the real risk.
- **No PVCs or PersistentVolumes** — it cannot touch storage, so it cannot destroy database data.
- **No nodes and nothing cluster-scoped** — it cannot drain or cordon the node, cannot see
  `kube-system`, and cannot reach any other namespace, including one created later.
- **No RBAC resources** — it cannot grant itself more permission, which is what makes every other
  restriction here durable rather than advisory.
- **No AWS permissions of any kind.** Sentinel runs as a pod with no IAM role; it cannot touch ECR,
  EC2, or anything else in the account.

**Application-level.** `citizen-postgres` and `notification-postgres` are not on the remediation
allow-list, and are additionally on a frozen deny-list that no environment variable can override.
Database incidents escalate to a human by design: restarting a Postgres pod under load is a
plausible-looking action that risks data loss and fixes almost nothing, and rolling one back is
meaningless.

**Chaos.** The `CHAOS_ADMIN_TOKEN` that lets Sentinel *reset* a fault is not a general grant to
*create* them. Sentinel gaining the ability to inject failures into the thing it protects is a
capability worth adding deliberately later, if ever — not one it should have by default.

---

## Deployment

### Local development

Unchanged from earlier phases, and the fastest way to see the application working. Pick the script
matching your cluster:

```bash
./scripts/deploy-docker-desktop.sh   # Docker Desktop's built-in Kubernetes
./scripts/deploy-kind.sh             # a local kind cluster
./scripts/deploy-minikube.sh         # a local minikube cluster
```

All three are idempotent. Then verify end to end, and drive an incident:

```bash
./scripts/smoke-test.sh
export CHAOS_ADMIN_TOKEN=<the token set in both services' Secrets>
./scripts/incident-scenarios.sh http-errors
./scripts/incident-scenarios.sh all
```

Applying the manifests by hand instead:

```bash
kubectl apply -k k8s/
kubectl get pods -n citizen-portal -w
```

That command is **unchanged** despite the manifests having moved into `k8s/base/` — see
[`k8s/README.md`](k8s/README.md), which also covers image naming, the hosts-file entry for
`citizen-portal.local`, port-forwarding the observability stack, and troubleshooting.

Sentinel is currently only in the AWS overlay and has no local-development equivalent yet; see
[Future roadmap](#future-roadmap).

Docker Compose still works too, for the application without Kubernetes — see
[`Phases.md`](Phases.md).

### AWS deployment

**This has not been done yet.** The steps below are what the code implements, not a record of a
successful run. Read [`docs/aws-deployment.md`](docs/aws-deployment.md) in full first, and expect
the first real `terraform plan` to surface something.

```bash
# 1. Provision. Terraform state is local — no S3 backend.
cd infra/terraform
terraform init
terraform plan
terraform apply

# 2. Configure GitHub: the OIDC role ARN and instance id as repository secrets,
#    and the aws-demo Environment. See docs/github-configuration.md.

# 3. On the node, over Session Manager — there is no SSH.
aws ssm start-session --target <instance-id> --region <region>
sudo /opt/sentinel-sre/scripts/generate-aws-secrets.sh
sudo /opt/sentinel-sre/scripts/deploy-aws.sh <git-sha>

# 4. Thereafter, a push to main deploys itself: tests → build → ECR → SSM.
```

Reaching the internal tools, once it is running, is a port-forward through Session Manager rather
than an exposed endpoint — the commands are in [`docs/aws-deployment.md`](docs/aws-deployment.md).

## Cost-conscious AWS architecture

The architecture is shaped by one constraint: this is a demonstration environment that needs to be
cheap enough to leave running while Sentinel is developed against it. Every declined AWS service
was declined for a stated reason, and each has a real tradeoff.

**EKS → K3s on EC2.** This is the big one, and it is purely about cost: EKS's managed control plane
is a standing charge that accrues before a single workload node exists, whereas K3s gives a
conformant Kubernetes API on an instance that has to exist anyway. What is given up is real — no
managed control plane, no control plane HA, no managed upgrade path, and the node is a single point
of failure. For a cluster whose purpose is to have something Sentinel can act on, that is the right
trade. For anything with users, it is not.

**RDS → PostgreSQL in Kubernetes.** This one is only partly about cost. **Database failure is one
of Sentinel's incident scenarios.** Moving Postgres to a managed service would put it outside the
cluster Sentinel observes and outside the RBAC boundary that makes Sentinel's behaviour analysable,
and would delete the most interesting case in the project: the one where the correct autonomous
action is *no action*. The cost is that there are no managed backups, no automated failover, and no
point-in-time recovery — see [Limitations](#limitations).

**ALB → Traefik on the node.** K3s installs Traefik anyway, and the instance's public IP is
directly reachable. An ALB would add a standing charge to route traffic to exactly one target.
What is given up: no managed TLS termination, no WAF integration, no health-check-based failover.

**NAT Gateway → a public subnet.** A NAT Gateway exists to give private-subnet workloads egress.
With one node that has to be reachable from the internet anyway, it buys nothing and bills
continuously. The node reaches ECR, SSM and `get.k3s.io` through the Internet Gateway using its own
public IP. The tradeoff is that the instance has a public IP at all — mitigated by there being
exactly one open port and no SSH.

**One instance, one AZ, no autoscaling.** Replica counts are sized by hand to fit 2 vCPU;
`MAX_REPLICAS=3` is what the node can absorb rather than a policy preference.

No dollar figures are quoted here on purpose. AWS pricing varies by region and changes over time,
and a number written into a README ages badly and gets quoted as authoritative. Price the shape of
this architecture — one burstable instance, one EBS volume, ECR storage, and data transfer —
against the AWS pricing pages for the region you actually deploy in.

## Limitations

Read this section before drawing any conclusion from anything above it.

**The AWS environment has not been deployed or verified end to end.** The Terraform is written and
`terraform validate` passes; **`terraform plan` has never been run**, because a plan needs real
credentials against a real account. No instance has been created. No pod has ever started on AWS.
Sentinel has never run against a real cluster. The autonomous remediation and rollback path is
implemented and unit-tested with the Kubernetes and Prometheus clients stubbed, and is **not
verified end-to-end**. Every claim in this README about Sentinel's behaviour is a claim about code,
not about observed behaviour.

**Single node, no redundancy.** One EC2 instance, one availability zone, one control plane. If the
instance goes, everything goes — the application, the observability stack, and Sentinel with it.

**Sentinel is a single replica and is not safely horizontally scalable.** Incident deduplication is
in-process and the incident store is node-local SQLite, so two replicas would each open their own
incident for the same alert and neither would see the other's history. Sentinel is therefore itself
a single point of failure in a system meant to be watching for single points of failure.

**No TLS and no DNS.** The portal is served over plain HTTP on an IP address. `:443` is wired
behind a Terraform variable that defaults to off because nothing terminates TLS yet. Doing this
properly needs a hostname first; a self-signed certificate on an IP address would be worse than
plain HTTP, because it trains whoever demos it to click through browser warnings.

**No database backups.** Postgres runs in-cluster on a local-path PVC with no `pg_dump` schedule,
no snapshot policy, and nothing that survives instance replacement. This is the sharpest edge of
the "not production" caveat.

**Terraform state is local.** No S3 backend and no lock table. Fine for one operator on one
laptop; a genuine problem the moment a second person or a CI job needs to apply.

**`local-path` storage does not survive instance replacement.** PVCs are directories on the node's
root volume. They survive pod recreation and reboot; a `terraform destroy`/`apply` starts from an
empty database.

**The LLM is optional, and Sentinel is rule-based without it.** With no `OPENAI_API_KEY` — the
default — Sentinel still detects, investigates, correlates, decides, remediates and validates, but
the root-cause narrative is rule-generated. Nothing degrades silently; the incident records
`llm_used=false`. But "AI-powered" is doing less work in that configuration than the phrase
suggests, and the honest framing is that the LLM enriches an analysis the rules already produce.

**Local development has no Sentinel.** Sentinel is only in the AWS overlay, so the fastest
environment to bring up is also the one where the agent cannot be exercised.

**ingress-nginx locally is unmaintained.** Fine for a laptop, and the reason AWS uses Traefik
instead. Not something to put in front of real traffic.

**No image vulnerability scanning**, for the supply-chain reasons documented in
[`Phases.md`](Phases.md)'s Phase 11 — which have not been revisited since.

## Future roadmap

Roughly in the order that would actually help:

1. **Apply the infrastructure and run one real incident end to end.** `terraform plan`, `apply`,
   deploy, then `./scripts/incident-scenarios.sh bad-deployment`, and watch Sentinel roll it back.
   Nothing else on this list matters until this happens.
2. **A hostname and real TLS**, which also unblocks exposing Grafana with real authentication
   instead of a port-forward.
3. **Remote Terraform state** (S3 + locking) before a second person touches the infrastructure.
4. **Scheduled `pg_dump` to S3** — the cheapest backup that is better than none.
5. **A local Sentinel setup**, so the agent can be exercised on kind/minikube without AWS.
6. **Make Sentinel horizontally scalable** by moving the incident store and dedup window out of the
   pod — worth doing only once there is more than one node for a second replica to land on.
7. **Synthetic incidents in CI** — run the chaos scenarios on a schedule and assert on Sentinel's
   incident records, not just on alerts firing.
8. **More remediation actions**, each one requiring an enum member, a Policy Engine branch, and
   possibly an RBAC verb. Deliberately not a configuration change.
9. **Multi-node, then multi-cluster.** Everything about the current design assumes one node, and
   several limitations above dissolve only when that stops being true.
10. **Predictive detection** — acting on a trend before an alert fires. Interesting, and much
    harder to bound safely than reacting to a fired alert, which is why it is last.

## Documentation map

| Document | What is in it |
|---|---|
| [`Phases.md`](Phases.md) | The full build history, phase by phase, with what was deferred and why at each step. The most honest document in the repo. |
| [`docs/aws-deployment.md`](docs/aws-deployment.md) | The AWS architecture in detail and the deployment runbook. |
| [`docs/github-configuration.md`](docs/github-configuration.md) | Repository secrets, the OIDC role, environments, branch protection. |
| [`docs/sentinel-integration.md`](docs/sentinel-integration.md) | What Sentinel reads, what it may act on, `request_id` correlation, and chaos-awareness. |
| [`k8s/README.md`](k8s/README.md) | Manifest layout, the base/overlay split, both deployment paths, troubleshooting. |
| [`scripts/`](scripts/) | `deploy-*.sh`, `deploy-aws.sh`, `generate-aws-secrets.sh`, `smoke-test.sh`, `incident-scenarios.sh`, `teardown.sh`. |
