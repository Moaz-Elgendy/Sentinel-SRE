# Sentinel incident engine

How Sentinel decides *which incident an event belongs to*, how several
incidents run at once without touching each other, and why escalation and retry
can never loop. Code: `sentinel-ai/app/lifecycle/incident_manager.py`,
`evidence_signature.py`, `orchestrator.py`.

## 1. Lifecycle (unchanged model, new guarantees)

Status (`IncidentStatus`): `open → investigating → remediating → validating →
resolved | escalated | auto_resolved`. `escalated` is a terminal state.
Phases (`LifecyclePhase`) give the finer steps on the timeline.

| Conceptual state | Where it lives |
|---|---|
| DETECTED | status `open`, phase `detection` |
| CORRELATED | phase `correlation` |
| INVESTIGATING | status `investigating` |
| RCA_COMPLETE | phase `root_cause_analysis` |
| DECIDED | phases `remediation_decision`, `policy_check` |
| REMEDIATING / VALIDATING / RESOLVED | statuses of the same name |
| ESCALATED | status `escalated` + `escalation_record` |
| FAILED | not a separate status: every bound ends in ESCALATED with an explicit `escalation_reason` |

No new statuses were added. Provider outages are *not* lifecycle states (§6).

## 2. Incident identity

`fingerprint` = `sha256(environment_id | application_id | namespace | app |
failure_class)`. Deliberately **not** included: timestamps, `startsAt`,
metric values, severity, the pod name, the Alertmanager fingerprint, the
alertname, and the RCA category.

* Alertmanager's fingerprint hashes every label including the pod, so it
  changes whenever a pod is replaced (which Sentinel's own restart does).
* Alert rules describing one symptom share a *failure class*
  (`HighHTTPErrorRate` + `ChaosForcedHTTPFailures` → `http_errors`; see
  `detection.failure_class_for`). Unknown alertnames get their own class.
* RCA category is an *output* of the lifecycle (and of an LLM), so it can
  never decide identity.

Different app ⇒ different incident, however close in time.

## 3. Correlation rules (`IncidentManager.handle_alert`)

Look at the **newest** incident with the same identity:

| Newest incident is… | Result |
|---|---|
| none | create |
| running in this process | join: counters only, no new work |
| `open/investigating/remediating/validating` but nothing running it | mark ESCALATED (`interrupted_by_restart`), then treat as escalated |
| `escalated` | join; **never re-escalate**; at most one evidence re-check (§4) |
| `resolved/auto_resolved` within `incident_recurrence_gap_seconds` (600s) | absorb (tail of the same occurrence; rate[5m] alerts outlive recovery) |
| `resolved/auto_resolved` after the gap | **new** incident, `occurrence+1`, `previous_incident_id` set |

Cooldowns are only a secondary guard; identity/state decide.

## 4. Escalation and reopening

On entering ESCALATED the orchestrator writes `escalation_record`: incident id,
timestamp, reason, RCA + confidence, every rejected action with its policy
denial reason and the confidence it needed, the evidence used, and an
*evidence signature* (the baseline).

A repeat alert on an escalated incident is compared to that baseline. It
reopens the **same** incident only if all hold:

1. escalation reason ∈ {`no_safe_action`, `low_confidence`, `unknown_alert`}
   and Sentinel has executed no action on it (otherwise: human only);
2. fresh evidence differs **materially** from the baseline;
3. `reopen_count < max_incident_reopens` (3);
4. at least `escalated_reconsider_min_interval_seconds` (600) since the last
   re-check (secondary safety).

**Material change** (`evidence_signature.py`): new deployment revision/image;
5xx error-rate band, p95 band, CPU band or restart band crossed; memory
doubled; health status or `up` flipped; replica availability changed; a new
Kubernetes Warning reason; a new (normalised) log-error pattern. *Not*
material: jitter within a band, the same log line more often, timestamps, pod
names, events ageing out. With no baseline the engine records one and does
nothing else (it never guesses "changed").

Human paths, not counted against the budget and needing no evidence change:
`POST /api/incidents/{id}/reinvestigate` (admin) and temporary authorization.
Both still go RCA → decision → **Policy Engine** → remediation.

## 5. Concurrency and persistence

* Each new incident is its own asyncio task (previously FastAPI
  `BackgroundTasks`, which Starlette runs one after another).
* **One writer per incident**: `IncidentManager._live` holds the single
  in-memory `Incident` whose lifecycle runs; repeats are folded into it. The
  authorization and re-run paths take the same lease.
* Remediation is serialised **per Deployment** (`asyncio.Lock`); after waiting,
  an incident whose evidence predates another incident's action re-investigates
  instead of acting on a stale picture. The Policy Engine's cooldown also sees
  actions by *other* incidents (`PolicyContext.other_incident_actions`).
* No shared decision state: learning bias is passed per call; cluster-wide
  notification series are collected only by the service that owns them.
* SQLite: one connection, one lock, WAL. Every write is short and serialised;
  whole-record upserts are last-writer-wins per incident, which the
  single-writer rule makes safe.
* `max_concurrent_lifecycles` (4) bounds concurrency.
* Startup (`recover_interrupted`): an in-flight incident that had executed
  nothing is resumed **once**; anything else becomes ESCALATED
  (`interrupted_by_restart`) and is never re-run automatically.

**Limitations.** In-process only (single replica): two replicas would need a
lease row in the store. A restart interrupts running lifecycles (handled as
above). When a sibling incident's remediation already fixed the shared
Deployment, the second incident re-investigates and usually escalates
"no safe action" rather than closing as resolved; Alertmanager's resolved
notification then closes it as `auto_resolved`.

## 6. Bounds (nothing loops)

| Path | Bound |
|---|---|
| re-investigation cycles in one run | `MAX_LIFECYCLE_CYCLES` (5) |
| executed actions per incident | `max_actions_per_incident` (3) |
| wall clock per run | `max_lifecycle_seconds` (1800), checked between cycles → `lifecycle_timeout` |
| automatic reopens | `max_incident_reopens` (3) → "retry limit" event |
| repeat alerts | never start work (counters only) |

## 7. Reasoner (LLM) failures

The LLM never creates or transitions an incident. A provider failure is a
*condition*: `ReasonerHealth` (per reasoner) counts failures; after
`reasoner_failure_threshold` (3) it skips the provider for
`reasoner_cooldown_seconds` (120). The analysis then runs rules-only,
`hypothesis.llm_status` is `call_failed` / `reasoner_unavailable`, the
timeline notes `REASONER_UNAVAILABLE`, confidence is not raised, and
`GET /api/activity/status` shows `reasoner.status`. It never becomes an
incident.

Gemini: REST `POST https://generativelanguage.googleapis.com/v1beta/models/<GEMINI_MODEL>:generateContent`,
key in the `x-goog-api-key` header. A non-200 logs `gemini_call_http_error`
(endpoint, model, API version, status, Google's error status/message,
request id, scrubbed body). On a 404 it also logs `gemini_available_models`
once. HTTP 404 here means the *model* is not found for that API version (a
retired/renamed/mistyped model), not a bad key (400/403). Providers:
`LLM_PROVIDER=openai|gemini|groq`; Groq needs `GROQ_API_KEY`.

## 8. Sentinel Live

Real backend events (`incident_updated`, with `event_kind`): created;
duplicate correlated / ignored; investigation started; Prometheus/Loki/
Kubernetes evidence gathered (with real values); re-check started / unchanged /
reopened / blocked by retry limit; plus every timeline entry (RCA, policy
verdicts, execution, validation, escalation, resolution).
`/api/activity/status` now also returns `active_incidents` and `reasoner`.

## 9. Self-monitoring scope

Reasoner failures are handled as above. Other Sentinel-internal or
monitoring-source failures (Prometheus down, Kubernetes unreachable) already
degrade to "collector error" in the evidence and cannot create incidents,
because incidents are only ever created from Alertmanager alerts. A dedicated
health view for those is not part of this change.
