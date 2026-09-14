# Sentinel AI Integration

Sentinel AI is an autonomous SRE agent, and as of Phase 13 it lives **in this repository**, at
[`sentinel-ai/`](../sentinel-ai/). Earlier versions of this document described it as a separate
platform in its own repo, with this project as its sample workload and this file as the *contract*
between the two. That split is gone. Sentinel is deployed by the AWS overlay into the same
`citizen-portal` namespace as the application it watches, and the "contract" is now simply the set
of signals it reads and the set of actions it is permitted to take.

What has not changed is why the application looks the way it does. Every observability building
block from Phases 6, 9 and 10 — structured logs with `request_id` correlation, Prometheus metrics,
Alertmanager rules, a token-authenticated chaos surface — exists specifically so Sentinel has
something real to detect, diagnose and act on, rather than a metric fabricated for a demo.

**Nothing here has been run against a live cluster yet.** Sentinel is implemented and unit-tested
with its Kubernetes and Prometheus clients stubbed; it has never observed a real incident. Read
this as a description of implemented behaviour, not verified behaviour.

## Phase 1: external control plane (this refactor)

Everything below this section describes the deployment topology as it existed before this
refactor — Sentinel as a pod inside the same cluster as the workload, with no AWS access and one
hardcoded set of connection strings. That is now one of three supported ways to run Sentinel, not
the only one, and two specific claims further down are now qualified rather than universally true:
"no AWS permissions of any kind" (see `app/clients/aws_client.py`, connector present but not yet
wired into evidence collection) and the implicit assumption that connection info is process-global
(it is now per-`Environment`; see below). Everything else on this page — the safety boundary, the
policy engine, the RBAC reasoning, the loop diagram, the chaos-awareness logic — is unchanged and
still accurate.

**What changed, and why.** Before this refactor, `sentinel-ai/app/core/config.py` *was* the
environment: one Prometheus URL, one Kubernetes ServiceAccount, hardcoded in-cluster Service DNS.
Every lifecycle module reached process-global settings for connection info. That is fine for "one
demo, one cluster, Sentinel deployed alongside it" but does not describe an external control plane
that is meant to watch a *remote* cluster it is not deployed inside.

The fix is `app/domain/environment.py`'s `Environment` model — customer id, connection config for
Kubernetes/Prometheus/Loki/GitHub/AWS, and a lightweight application profile — plus wiring
`lifecycle/orchestrator.py:build_context()` to construct its `SentinelContext` **from an
`Environment`**, not from raw settings. Settings still supply policy thresholds, validation bounds
and execution mode (`dry_run` etc.) — none of which are customer-specific in this phase — but every
connector now gets its connection info from the registered `Environment`.

```text
BEFORE                              AFTER
                                     Environment (Customer, connection config)
Settings ──> SentinelContext              │
                                           v
                                     SentinelContext (built FROM the Environment)
```

**Kubernetes connectivity — the most important externalisation.** `KubernetesClient.initialise()`
(`sentinel-ai/app/clients/kubernetes_client.py`) now supports three modes, selected by
`KUBERNETES_MODE`:

| Mode | What it does | When to use it |
|---|---|---|
| `in_cluster` (default) | The original behaviour: an in-cluster ServiceAccount. | Sentinel deployed inside the same cluster as the workload — unchanged from before this refactor. |
| `kubeconfig` | Decodes a base64 kubeconfig YAML (`KUBERNETES_KUBECONFIG_B64`) via `load_kube_config_from_dict`. | Sentinel running as an external control plane against a remote K3s/K8s API server. **This is the mode that makes the architecture in the spec real.** |
| `remote` | Explicit `KUBERNETES_API_SERVER` + `KUBERNETES_TOKEN` + `KUBERNETES_CA_CERT_B64`, built into a `Configuration` object. | Same as `kubeconfig`, for environments where handing over a full kubeconfig is undesirable — hand over a single scoped token instead. |

Whichever mode is used, the credential should be scoped to exactly the Role in
[`namespace-rbac.yaml`](../k8s/overlays/aws/sentinel/namespace-rbac.yaml) — see
`REQUIRED_RBAC` at the bottom of `kubernetes_client.py`, kept in sync with that manifest by hand.
`kubeconfig`/`remote` mode need no Kubernetes-side change at all: the *credential* changes (a token
handed to Sentinel instead of a mounted ServiceAccount), not the RBAC Role it is scoped to.

Prometheus and Loki gained an optional bearer-token header (`PrometheusClient`/`LokiClient`
constructors) for the same reason: reached through an Ingress instead of an in-cluster `ClusterIP`
Service, they may need auth Sentinel didn't previously send.

**Multi-tenancy identifiers.** `Incident` now carries `customer_id` / `environment_id` /
`application_id`, stamped by `routers/alerts.py` from `request.app.state.environment` right after
`detection.build_incident()` creates the incident. Phase 1 registers and uses exactly one
environment — see "Known limitations" below for what routing an inbound webhook to *one of several*
environments would need, which is deliberately not built yet.

**AI provider abstraction.** `rca.py`'s direct `openai.AsyncOpenAI` call moved to
`app/reasoning/` — a `Reasoner` ABC (`base.py`) with `OpenAIReasoner` (unchanged behaviour, also
serves Groq/OpenRouter via `OPENAI_BASE_URL`) and `GeminiReasoner` (plain REST over `httpx`, no new
SDK dependency) implementations, selected by `LLM_PROVIDER` via `app/reasoning/factory.py`. The
trust boundary this refactor deliberately did NOT touch: `rca.apply_llm_response()` still validates
whatever a Reasoner returns against the rule engine's own conclusion before any of it is used, and
a `Reasoner` still returns a raw string — never an action, never a capability.

**Environment registration API** (`app/routers/environments.py`):

```text
POST /environments                       register (and, in Phase 1, activate) an environment
GET  /environments                       list registered environments (redacted)
GET  /environments/{id}                  get one (redacted)
POST /environments/{id}/test-connection  probe each configured connector
POST /environments/{id}/discover         list deployments/services/pod count in its namespace
```

Every response is redacted through `Environment.to_public_dict()` — no kubeconfig, token, or
bearer/access key ever appears in an API response.

### How to run Sentinel outside the cluster

1. Get a scoped credential for the remote cluster: either a kubeconfig whose only permissions are
   the `namespace-rbac.yaml` Role (`kubectl create token sentinel-ai -n citizen-portal
   --duration=8760h` plus a kubeconfig pointing at it, or equivalent), or just that token plus the
   cluster's CA cert.
2. Set `KUBERNETES_MODE=kubeconfig` and `KUBERNETES_KUBECONFIG_B64=$(base64 -w0 kubeconfig.yaml)`
   (or `KUBERNETES_MODE=remote` with `KUBERNETES_API_SERVER`/`KUBERNETES_TOKEN`/
   `KUBERNETES_CA_CERT_B64`).
3. Set `PROMETHEUS_URL`/`LOKI_URL` to however Sentinel reaches them from outside the cluster — an
   Ingress hostname, a VPN address, an SSM-tunnelled port-forward — not the in-cluster DNS name.
4. Run `sentinel-ai` anywhere with network access to that API server and those URLs (a laptop, a
   separate EC2 instance, a different cluster entirely). `docker run` or `python -m uvicorn
   app.main:app` both work — nothing in the image assumes it is inside the cluster it watches.
5. Point the remote cluster's Alertmanager at Sentinel's `/api/alerts/webhook`, reachable however
   step 4's network path allows.
6. Confirm connectivity: `curl -X POST http://<sentinel-host>:8080/environments/demo-env/test-connection`
   (or the id returned by `GET /environments` if you registered one explicitly via `POST
   /environments` instead of relying on the env-var bootstrap).

### How to register a remote environment via the API

Rather than the env-var bootstrap (which seeds exactly one `Environment` from `CUSTOMER_ID`/
`KUBERNETES_MODE`/etc. at startup if the store is empty), register one directly:

```bash
curl -X POST http://localhost:8080/environments -H 'Content-Type: application/json' -d '{
  "name": "acme-prod",
  "customer_id": "acme-corp",
  "kubernetes": {"mode": "kubeconfig", "namespace": "citizen-portal", "kubeconfig_b64": "'"$(base64 -w0 kubeconfig.yaml)"'"},
  "prometheus": {"url": "https://prometheus.acme.example.com"},
  "loki": {"url": "https://loki.acme.example.com"},
  "github": {"token": "ghp_...", "repository": "acme/citizen-portal"},
  "application": {"id": "citizen-portal", "name": "Citizen Portal", "deployments": ["citizen-service", "notification-service", "frontend"]}
}'
```

This immediately rebuilds Sentinel's active `SentinelContext` from the new environment — see the
Phase 1 simplification noted in `routers/environments.py`'s module docstring: registering a second
environment *replaces* the active one rather than running alongside it.

## Where Sentinel runs

A single-replica FastAPI deployment on the K3s node when deployed in-cluster (`KUBERNETES_MODE=
in_cluster`, the AWS overlay's default), listening on `:8080`, receiving Alertmanager webhooks at
`/api/alerts/webhook`. It is `ClusterIP`-only in that topology — not exposed publicly, reachable
from a laptop only by port-forwarding through AWS Systems Manager Session Manager. Run externally
instead (`kubeconfig`/`remote` mode — see "Phase 1: external control plane" above) and Sentinel can
be anywhere with network access to the remote cluster's API server and observability stack; how it
is exposed to that cluster's Alertmanager becomes a deployment-specific choice rather than this
fixed shape.

Its Kubernetes access is a dedicated ServiceAccount bound to a **namespaced `Role`** in
[`k8s/overlays/aws/sentinel/namespace-rbac.yaml`](../k8s/overlays/aws/sentinel/namespace-rbac.yaml)
— not a `ClusterRole`, and not `cluster-admin`. When deployed this way (in-cluster), it has **no
AWS permissions of any kind**: it runs as a pod with no IAM role, so it cannot reach ECR, EC2, or
anything else in the account, and there is no CloudWatch, no Amazon Managed Prometheus, and no IRSA
anywhere in this topology — see "Phase 1: external control plane" above for the AWS connector that
exists for other deployment modes (not yet wired into evidence collection either way).

Incident history is SQLite on a node-local volume, which is also why there is exactly one replica:
deduplication is in-process, so a second replica would open its own incident for the same alert and
see none of the first one's history.

## What Sentinel can read

| Signal | Where | Access pattern |
|---|---|---|
| Metrics | Prometheus, in-cluster (Phase 9) | PromQL over HTTP to `http://prometheus:9090` |
| Logs | Loki (Phase 9), structured JSON with `request_id`, `level`, `service` labels (Phase 6) | LogQL over HTTP to `http://loki:3100` |
| Alerts (push, real-time) | Alertmanager webhook receiver | Alertmanager `POST`s to `http://sentinel-ai:8080/api/alerts/webhook` — this is the primary "wake up" signal, not polling |
| Kubernetes state | The K3s API server | Namespaced `Role`: pods, services, endpoints, configmaps, events, deployments, replicasets, pod logs |
| Deployment history | Deployment + ReplicaSet objects | The `deployment.kubernetes.io/revision` annotation *is* the rollout history, and each ReplicaSet holds the exact pod template — and so the exact image, and so the exact git SHA — of a past revision |
| HTTP health | `/readyz` on each service | Parsed as **JSON**, not by status code: `/readyz` returns 200 with `status: "degraded"` when a downstream dependency is broken |
| Deliberate chaos state | `chaos_*` Prometheus metrics (Phase 10, extended in Phase 13): `chaos_latency_ms`, `chaos_error_rate`, `chaos_db_failure`, `chaos_cpu_burn`, `chaos_memory_leak_mb`, `chaos_notification_failure_rate`, `chaos_injections_total` | Same PromQL path as any other metric — see "Chaos-awareness" below for why this matters specifically |

Two of those rows deserve emphasis because they are the ones that make correlation possible rather
than merely plausible.

**ReplicaSet history is what turns "was it a deploy?" into a mechanical question.** Reading the
revision annotations tells Sentinel when the current revision was created and what the previous one
was — which is both how it correlates an incident with a deployment and how it knows whether a
rollback has anywhere to go. This is why the AWS overlay sets `revisionHistoryLimit` explicitly and
why CI deploys with `kubectl set image` (which produces a clean new ReplicaSet) rather than a bare
`apply`.

**`request_id` is the thread that ties the rest together.** It is generated per-request in
`citizen-service`'s middleware (Phase 6), attached to every log line for that request, and
propagated to `notification-service`. It is what lets a spike in `HighHTTPErrorRate` be correlated
back to specific log lines — and from there to the specific citizen action that caused it — instead
of Sentinel only ever seeing aggregate rates with no way to drill in.

## What Sentinel can act on

Read access is unrestricted within its namespace. Write access is four actions, each with a
confidence threshold, all restricted to an allow-list:

| Action | Threshold | Mechanism |
|---|---|---|
| `restart_deployment` | ≥ 0.90 | Patches the pod template annotation — what `kubectl rollout restart` does. No pod is deleted, so no `delete` verb is granted anywhere. |
| `rollback_deployment` | ≥ 0.95 | Patches the pod template back to a previous ReplicaSet's template. |
| `scale_deployment` | ≥ 0.90 | Patches replicas, bounded to 1–3. **Never 0** — that is `scripts/incident-scenarios.sh`'s `full-outage` scenario deliberately causing an outage, and is never a fix. |
| `reset_chaos_fault` | ≥ 0.90 | `POST /api/chaos/reset` — the correct action when the diagnosis is that the "incident" is a deliberately injected fault, rather than paging anyone. |

The allow-list is `citizen-service`, `notification-service`, `frontend`, in namespace
`citizen-portal`. **`citizen-postgres` and `notification-postgres` are not on it**, and are
additionally on a frozen deny-list that no environment variable can override. A database incident
escalates to a human, always. Restarting a Postgres pod under load is a plausible-looking action
that risks data loss and fixes almost nothing; rolling one back is meaningless.

### Rollback is autonomous

Earlier revisions of this document said rollback was "a plan Sentinel proposes, not an action it
takes unattended". **That is no longer true, and the change is deliberate.** Sentinel rolls back a
bad deployment with no human approval step. That is the point of the project.

What bounds it is not an approval gate but a set of preconditions that all have to hold, none of
which the language model can influence:

- The deployment and namespace are allow-listed (and not on the frozen deny-list).
- Confidence is at least 0.95.
- A previous revision actually exists.
- Deployment history exists to identify it.
- A recent deployment correlates with the incident onset, inside a configured window (30 minutes
  by default). Without this, every incident gets blamed on the last deploy.
- The rollback is reversible.
- Recovery validation is available for the target. If Sentinel cannot verify that the rollback
  worked, it does not perform it.

If any precondition fails, the Policy Engine rejects the action and the next candidate on the
Decision Engine's ordered list is tried — or, if none remains, the incident escalates.

The layering is what makes this defensible:

```text
LLM  ->  Decision Engine  ->  Policy Engine  ->  Remediation Engine  ->  Kubernetes API
```

The LLM analyses evidence and returns a *structured* recommendation: an action name from a fixed
enum, a target, a confidence score, reasoning. It never receives shell access, never receives
`kubectl`, and cannot name a target outside the allow-list. Everything downstream of it is
deterministic code, and the RBAC Role is a fourth line that holds even if the first three contain
bugs.

`DRY_RUN=true` runs the entire lifecycle and logs the action it *would* have taken without touching
the cluster. That is the honest way to demonstrate this to someone not yet comfortable with an
agent acting unattended. It is not the default.

## Chaos-awareness: telling a real incident from a deliberate one

This project is the one place Sentinel will regularly see *deliberately injected* failures (Phase
10's chaos API, driven by `scripts/incident-scenarios.sh`) alongside genuine ones. A Sentinel that
cannot tell them apart would either page on every test run or, worse, learn to ignore the alert
pattern that real incidents also produce.

The `chaos_*` metrics exist specifically to make this distinguishable, and correlation checks them
before treating any alert as a genuine unknown incident:

- `chaos_db_failure == 1` alongside `ChaosDatabaseFailure` and `HighHTTPErrorRate` firing is a
  strong signal this is a known, deliberate condition.
- `chaos_error_rate > 0` explains a 5xx spike; `chaos_latency_ms > 0` explains a latency alert.
- `chaos_cpu_burn == 1` explains a `HighCPUUsage` alert, and `chaos_memory_leak_mb > 0` explains
  `MemoryLeakSuspected` — both added in Phase 13 precisely because resource-exhaustion incidents
  are otherwise very easy to misdiagnose as a real leak in application code.
- All gauges at rest, with a new ReplicaSet created shortly before the onset, points the other way:
  a real incident, caused by a deploy.

When the diagnosis is a deliberate fault, the correct action is `reset_chaos_fault`, not an
escalation and not a restart — clearing the fault resolves the incident, and the incident record
says so.

The reverse case matters just as much. The `chaos_*` gauges being at rest is part of the *positive*
evidence for a real incident: it is how `bad-deployment` gets diagnosed as a bad deployment rather
than as an unexplained error spike.

## The loop

```mermaid
sequenceDiagram
    participant AM as Alertmanager
    participant S as Sentinel
    participant P as Prometheus / Loki
    participant K as K3s API
    participant Out as GitHub / Slack / incident store

    AM->>S: webhook: HighHTTPErrorRate firing (citizen-service)
    activate S
    Note over S: DETECTION — incident opened (deduped by window)

    S->>P: PromQL: error rate, latency, CPU, memory, chaos_* over the window
    S->>P: LogQL: ERROR/WARN logs for citizen-service, same window
    S->>K: pod status, recent Events, Deployment + ReplicaSet history
    Note over S: INVESTIGATION then CORRELATION —<br/>chaos_* at rest? new revision just before onset?
    Note over S: ROOT CAUSE ANALYSIS — hypothesis + confidence<br/>(LLM enriches; rules stand alone without it)

    alt Deliberate chaos fault
        Note over S: DECISION: reset_chaos_fault
        S->>K: POST /api/chaos/reset
    else Recent deploy correlates, all preconditions hold
        Note over S: DECISION: rollback_deployment (>= 0.95)
        S->>K: patch pod template to previous ReplicaSet's template
    else Resource exhaustion, no deploy correlation
        Note over S: DECISION: restart_deployment (>= 0.90)
        S->>K: patch pod template annotation
    else Database, unknown pattern, or preconditions fail
        Note over S: No permitted action remains
        S->>Out: ESCALATE — full diagnosis, no action taken
    end

    S->>K: emit Kubernetes Event recording the action
    S->>P: RECOVERY VALIDATION — availability, readiness, /readyz JSON,<br/>5xx rate, p95, CPU, memory, chaos_*
    alt Validation passes
        S->>Out: DOCUMENTATION + NOTIFICATION — RESOLVED
    else Validation fails
        Note over S: Re-investigate, take the next candidate action,<br/>re-validate. On exhaustion or action cap: escalate.
    end
    deactivate S
```

The full eleven-phase lifecycle (DETECTION through LEARNING) is described in
[`../README.md`](../README.md), along with a step-by-step worked example of the `bad-deployment`
scenario.

Two things about the loop are worth stating explicitly:

**Detection is push, not poll.** Alertmanager's webhook is the trigger. Sentinel does not
continuously poll Prometheus hoping to notice something; it is told, and *then* pulls the
underlying series — the actual shape of the metric, not just the alert's boolean firing state,
because severity and trend are what distinguish "briefly touched a threshold" from "getting worse".

**Every action is followed by validation, and RESOLVED means validated.** Not "the alert stopped
firing" — the full check set has to pass, including the `/readyz` body parse that catches a service
which is up but degraded.

## Security boundary

- **Least-privilege RBAC.** A namespaced `Role`, not a `ClusterRole`. Reads pods, services,
  endpoints, configmaps, events, deployments, replicasets and pod logs; writes only
  `patch`/`update` on `deployments` and `deployments/scale`, plus `create` on Events. It cannot
  exec into a container, cannot delete anything, cannot read Secrets, cannot touch PVCs or nodes,
  and cannot modify RBAC. The full reasoning for each omission is in the comments in
  [`namespace-rbac.yaml`](../k8s/overlays/aws/sentinel/namespace-rbac.yaml).
- **No Secrets access, specifically.** Sentinel has no reason to read application credentials, and
  not being able to means a confused or compromised agent cannot leak them into an incident report,
  a GitHub issue, or an LLM prompt. That last one is the real risk.
- **AWS credentials, when used at all, are least-privilege and read-only.** In-cluster deployment
  mode uses none (pod with no IAM role). The optional `AWSClient` connector (EC2 + CloudWatch,
  read-only, not yet wired into evidence collection) needs at most `ec2:DescribeInstances` +
  `cloudwatch:GetMetricStatistics` — never write access to anything in the account.
- **Actions are auditable in the cluster's own surface.** Every action emits a Kubernetes Event, so
  it appears in `kubectl get events` alongside everything else that happened. An autonomous agent
  that acts without leaving a trace where operators already look would be much harder to trust or
  debug.
- **`CHAOS_ADMIN_TOKEN` is not a general grant.** Sentinel can *reset* a fault it correctly
  diagnosed as deliberate; the ability to *create* faults stays an operator/CI concern. Sentinel
  gaining the ability to inject failures into the thing it is supposed to be protecting is a
  capability worth adding deliberately later, if ever — not one it should have by default.
- **GitHub integration writes, but not to code.** Sentinel creates issues, incident reports and
  postmortems, and may *propose* a fix. It never modifies application code and never merges
  anything.

## Testing the loop

`scripts/incident-scenarios.sh` is the test harness for Sentinel itself, not just for this
project's alerting. Nine scenarios exist — `db-outage`, `http-errors`, `latency`,
`notification-degradation`, `full-outage`, `high-cpu`, `memory-leak`, `crashloop`,
`bad-deployment` — and four of them were added in Phase 13 specifically because they are the cases
where Sentinel's *choice of action* is the interesting part rather than its detection:

| Scenario | The correct behaviour, and why it is a test of judgement |
|---|---|
| `high-cpu` | Restart. A CPU-burning process is not fixed by rolling back code that did not change. |
| `memory-leak` | Restart. Same reasoning, slower onset. |
| `crashloop` | *Not* a restart — the pod is already restarting. Correlate with the deploy or escalate. |
| `bad-deployment` | Rollback, and only rollback. A restart brings the same broken code back up; scaling produces more of it. |
| `db-outage` | **Escalate.** Postgres is not remediable. The correct autonomous action is no action. |

The sequence to run, once there is a live cluster: trigger a scenario, then confirm Sentinel
detected it, diagnosed it correctly (including correctly recognising a deliberate chaos fault as
deliberate), took the right action or correctly escalated, and validated the recovery — before
ever pointing it at a real, unplanned failure.

That sequence has not been run. It is the single most important open item in the project; see
[`../Phases.md`](../Phases.md)'s Phase 13 "What's missing" and `## What's next`.

## This refactor's diff, in full

**New files:**

| File | Purpose |
|---|---|
| `sentinel-ai/app/domain/environment.py` | `Environment`/`Customer` model, connector configs, redaction |
| `sentinel-ai/app/routers/environments.py` | `POST/GET /environments`, `test-connection`, `discover` |
| `sentinel-ai/app/reasoning/base.py` | `Reasoner` ABC |
| `sentinel-ai/app/reasoning/openai_reasoner.py` | Extracted, unchanged OpenAI-compatible implementation |
| `sentinel-ai/app/reasoning/gemini_reasoner.py` | Gemini via plain REST (`httpx`), no new SDK |
| `sentinel-ai/app/reasoning/factory.py` | Selects a `Reasoner` from `LLM_PROVIDER` |
| `sentinel-ai/app/clients/aws_client.py` | EC2 + CloudWatch, read-only — **not yet wired into evidence collection**, see below |

**Modified files:**

| File | Change |
|---|---|
| `sentinel-ai/app/core/config.py` | `KUBERNETES_MODE`/kubeconfig/remote fields, `CUSTOMER_ID`/`ENVIRONMENT_ID`/`APPLICATION_ID`, `LLM_PROVIDER`+Gemini fields, Prometheus/Loki bearer tokens, AWS region/keys/instance-ids |
| `sentinel-ai/app/clients/kubernetes_client.py` | `initialise()` supports `in_cluster`/`kubeconfig`/`remote`; added `list_deployments`/`list_services` for discovery; `services: get/list` added to `REQUIRED_RBAC` |
| `sentinel-ai/app/clients/prometheus.py`, `loki.py` | Optional `bearer_token` constructor arg |
| `sentinel-ai/app/lifecycle/rca.py` | `enrich_with_llm()` takes a `reasoner: Reasoner \| None` instead of `api_key`/`model`/`base_url` — `apply_llm_response()` (the trust boundary) is byte-for-byte unchanged |
| `sentinel-ai/app/lifecycle/orchestrator.py` | `SentinelContext` gains `environment`/`reasoner` fields; `build_context()` takes an `Environment` and constructs every connector from it instead of raw settings |
| `sentinel-ai/app/models/incident.py` | `customer_id`/`environment_id`/`application_id` fields + `to_dict()` |
| `sentinel-ai/app/store/sqlite_store.py` | `environments` table + `upsert_environment`/`get_environment`/`list_environments` |
| `sentinel-ai/app/routers/alerts.py` | Stamps `customer_id`/`environment_id`/`application_id` onto each new incident from `app.state.environment` |
| `sentinel-ai/app/main.py` | Bootstraps the Phase-1 demo `Environment` from settings if the store is empty; registers `environments.router` |
| `sentinel-ai/requirements.txt` | `boto3` (optional, only imported when `AWS_REGION` is set) |
| `k8s/overlays/aws/secrets/sentinel.env.example` | Documents every new variable |
| `k8s/overlays/aws/sentinel/namespace-rbac.yaml` | Clarifying comment: this Role is for `KUBERNETES_MODE=in_cluster` specifically, one of three supported modes now |

**No database/schema migration was needed.** `incidents.body` and the new `environments.body` are
both JSON blobs (see `sqlite_store.py`'s `CREATE TABLE` statements) — new fields on `Incident`/
`Environment` just appear in newly-written rows; nothing had to be backfilled or altered.

**Test status:** all 144 existing tests pass unmodified (`pytest -q` from `sentinel-ai/`). The
refactor was additionally exercised by hand: booting the app end-to-end via `TestClient` (confirms
the environment bootstrap, `build_context` wiring, and router registration all work together, not
just in isolation), registering environments and confirming credential redaction in every response,
and constructing `KubernetesClient` in both `kubeconfig` and `remote` mode against a synthetic
kubeconfig/token — both build a client successfully and fail gracefully (returns `None`, logs a
warning) against an unreachable server, matching the pre-refactor fail-soft contract. **None of
this was run against a real remote K3s cluster** — no cluster was available in the environment this
refactor was written in. That is the load-bearing gap before calling this demo-ready; see below.

## Required credentials / access, by integration

| Integration | What Sentinel needs | Where it's scoped |
|---|---|---|
| Kubernetes (remote) | A kubeconfig or bearer token bound to the `sentinel-ai` Role — pods/services/endpoints/configmaps/events (read), deployments/replicasets/statefulsets/daemonsets (read), pods/log (read), deployments + deployments/scale (patch/update), events (create) | `namespace-rbac.yaml`, namespaced `Role`, not `ClusterRole` |
| Prometheus / Loki | Network reachability + optional bearer token if fronted by auth | Read-only HTTP; no write path exists in either client |
| GitHub | Fine-grained token: `contents: read` + `issues: write` on exactly `GITHUB_REPOSITORY`, or classic `repo` scope | `GitHubClient` never calls a write endpoint other than `POST .../issues` |
| AWS (optional, connector present, not yet wired) | If used: an IAM principal with `ec2:DescribeInstances` + `cloudwatch:GetMetricStatistics` only, or leave `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` empty and let boto3's default credential chain (env, shared config, instance/IRSA role) resolve it | `AWSClient` makes no other API calls |

## Known limitations

1. **Not run against a real remote cluster.** Every claim above about `kubeconfig`/`remote` mode is
   verified structurally (the client builds successfully, fails gracefully against an unreachable
   server) but not against a real K3s API server reached over a network. This is the next thing to
   do before calling the external-control-plane demo credible.
2. **Single active environment.** `POST /environments` replaces the active `SentinelContext`
   rather than running several concurrently, and there is no logic to route an inbound Alertmanager
   webhook to the *correct* one of several registered environments. Both are natural next steps —
   the `Environment`/`Incident` identifiers already exist specifically so adding them later does not
   require touching the `customer_id`/`environment_id`/`application_id` plumbing again — but neither
   is built.
3. **Policy Engine is still process-global, not environment-scoped.** `PolicyConfig` (allow-lists,
   confidence thresholds, denied deployments) comes from `Settings`, the same for every environment.
   Fine when there is one; wrong the moment two environments need different allow-lists. Deferred
   until a second real environment exists to design the scoping against, rather than guessing at a
   shape now.
4. **AWS connector exists but is not wired into evidence collection.** `AWSClient` genuinely calls
   EC2/CloudWatch and returns real data given credentials, but `lifecycle/investigation.py` does not
   call it yet and the Evidence Package has no AWS field. Wiring it in is one new field + one guarded
   call, matching every other collector's shape — deliberately deferred per the explicit
   prioritisation ("do not let a full AWS integration delay the primary demo").
5. **GitHub commit correlation is unchanged, not newly built.** It already existed before this
   refactor (`GitHubClient.get_commit`, used in `correlation.py`) and continues to work exactly as
   before — this refactor only changed *how the client is constructed* (from `environment.github`
   instead of raw settings), not what it does.
6. **Discovery is shallow, deliberately** (spec section 20's explicit instruction): namespace-scoped
   deployments/services/pod-count only. No dependency graph, no traffic-pattern modelling, no
   service-mesh topology.

## Next steps toward replacing the external LLM with a standalone Sentinel model

The seam is already in place: `Reasoner` is the only interface `rca.py` talks to, and every
incident's evidence, diagnosis, action, policy decision, remediation result and validation result
is already recorded in full (`IncidentMemory`/`sqlite_store.py`, unchanged by this refactor). The
concrete next steps, in order:

1. Let the incident dataset accumulate across real (or at minimum realistic chaos-scenario)
   incidents — the JSON blob per incident is already close to a training example; `lifecycle/
   learning.py` is the natural place to add an export that reshapes it into
   `(evidence_package, diagnosis, action, outcome)` tuples.
2. Add a `LocalSentinelReasoner(Reasoner)` in `app/reasoning/` once there is a model to call — same
   `complete_json(system_prompt, user_prompt) -> str | None` contract as `OpenAIReasoner`/
   `GeminiReasoner`, so `factory.py` gains one more branch and nothing else in the codebase changes.
3. Fine-tune against the accumulated dataset once there is enough of it to be worth doing — the spec
   is explicit that this was out of scope for the two-week prototype, and nothing in this refactor
   tried to start it early.

