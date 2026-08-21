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

## Where Sentinel runs

A single-replica FastAPI deployment on the K3s node, listening on `:8080`, receiving Alertmanager
webhooks at `/api/alerts/webhook`. It is `ClusterIP`-only — not exposed publicly, reachable from a
laptop only by port-forwarding through AWS Systems Manager Session Manager.

Its Kubernetes access is a dedicated ServiceAccount bound to a **namespaced `Role`** in
[`k8s/overlays/aws/sentinel/namespace-rbac.yaml`](../k8s/overlays/aws/sentinel/namespace-rbac.yaml)
— not a `ClusterRole`, and not `cluster-admin`. It has **no AWS permissions of any kind**: it runs
as a pod with no IAM role, so it cannot reach ECR, EC2, or anything else in the account. There is
no CloudWatch, no Amazon Managed Prometheus, and no IRSA anywhere in this design — the cluster is
K3s on one EC2 instance, and every signal Sentinel reads comes from an in-cluster service.

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
- **No AWS credentials.** Sentinel is a pod with no IAM role.
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
