# Sentinel SRE — Phase 2 + Phase 3 + Phase 4 Report

**Scope note (read first):** This work was scoped by two explicit decisions the
requester made before implementation began, in response to a gap analysis
presented in chat:

1. **Phase 3 approach:** keep the existing deterministic, single-round parallel
   evidence collector and harden/document it, rather than build a new
   adaptive, hypothesis-driven, multi-round LLM tool-calling loop. The brief
   itself (§19) explicitly permits this: *"Do not introduce unnecessary agent
   complexity if deterministic investigation orchestration is more
   reliable."*
2. **Live testing:** implement the code and run the full automated test suite
   only. Do **not** claim or imply that Live Tests A/B/C were run against the
   real AWS-hosted K3s cluster. This session has no reachable kubeconfig,
   `kubectl`/`aws`/`terraform` binaries, or populated `.env`/API keys for
   `sentinel-ai` (only `.env.example` templates exist) — so live-cluster
   verification could not honestly be performed here. Live Test sections
   below are filled with what the *code path* does, and explicitly marked
   NOT PERFORMED where the brief asks for live evidence.

The repository audit (Step 1/2 of the brief's own process) found that the
overwhelming majority of what the brief frames as "to implement" already
existed to a mature, well-tested standard from prior work on this codebase:
rollback target safety, RCA's rules-first cascade with capped LLM confidence,
scenario isolation, per-target concurrency, parallel evidence collection with
granular audit events, and layered recovery validation including a genuine
error-rate before/after. The work in this phase is therefore a **hardening
and gap-closure pass**, not a rewrite — consistent with the brief's own §43
("avoid unnecessary infrastructure") and §19 guidance.

---

## Architecture Changes

```text
No structural/architectural changes were made. The existing lifecycle
pipeline (detect -> correlate -> investigate -> RCA -> policy -> remediate
-> validate -> [re-investigate|escalate|resolve]) is unchanged, and every
existing safety control listed in the brief was preserved verbatim:
allow-lists, the Postgres/database deny-list, protected-resource list,
confidence thresholds (rollback >= 0.95, restart >= 0.90, scale >= 0.90),
bounded scale range, action cooldowns, max actions per incident, DRY_RUN,
typed remediation actions, deterministic policy validation, recovery
validation, RBAC scoping, audit logging, incident dedup/correlation,
per-target concurrency isolation, bounded retries/re-investigation,
terminal escalation behavior, provider-failure handling, and the hard
LLM/action separation (the LLM adjusts confidence/root-cause within capped
bounds; it never selects or executes an action, and it never gets direct
kubectl/shell/HTTP access).

Changes made were additive and backward-compatible:
  - New optional trailing parameters on validation check functions and on
    RecoveryValidator.validate()/_single_pass(), all defaulting to None.
  - Two new call sites in orchestrator.py pass the incident's pre-action
    Evidence (already collected) into validation as the "before" baseline.
  - A new named constant + documentation block in investigation.py.
  - A new regression test guarding scenario isolation.
  - A new test covering genuine (never-fabricated) before/after reporting.
  - Two test doubles (FakeValidator in tests/engine_harness.py, and an
    inline monkeypatched _fake_validate in tests/test_authorizations.py)
    updated to accept the new optional keyword arguments so existing tests
    continue to exercise the real call signature.

No new services, no new dependencies, no schema/migration changes.
```

## Phase 2

```text
Implemented:
Phase 2's safety-critical requirements were already implemented and already
covered by an extensive existing test suite (test_kubernetes_client.py,
test_correlation.py) before this pass, specifically:
  - Rollback candidate filtering: same deployment, older revision than
    current, existed before incident onset (correlation.py,
    _find_valid_rollback_candidate()).
  - Image validation for BOTH containers and initContainers
    (kubernetes_client.py, _assert_valid_container_images()), rejecting
    unsubstituted template placeholders (ACCOUNT_ID / REGION / PLACEHOLDER)
    as exact path segments (re.split on "./:@", never a substring match, so
    a legitimate image such as "my-region-service:v2" is not rejected).
  - A typed InvalidRollbackTemplate exception raised before any Deployment
    mutation is attempted.
  - A structured "rollback_target_selected" audit event emitted by
    orchestrator.py before the patch, plus rollback_candidates_skipped
    tracking for audit visibility into rejected candidates.
  - Scenario/chaos isolation: chaos_scenarios.py (the operator-triggered
    demo runner that invokes AWS SSM to run incident-scenarios.sh on the
    real K3s node) owns the SCENARIOS metadata exclusively; RCA
    (rca.py) and correlation (correlation.py) reason only from real
    metrics/logs/k8s state/events and never read scenario name/id/expected
    outcome/test-instruction fields.
  - Database/dependency-outage handling: Postgres and other declared
    dependencies are on a deny-list Sentinel never mutates; the only
    available response is investigate + escalate, never an autonomous
    schema/data/service change.

Gap found and closed:
  - No automated test previously asserted that RCA/correlation source code
    is free of scenario-metadata references. This was a real, if narrow,
    regression risk (a future edit could silently reintroduce a
    scenario-aware shortcut). Closed with a new structural guard test.

Files changed:
  tests/test_rca.py
    - Added imports: `inspect`, `app.lifecycle.correlation as correlation_module`,
      `app.lifecycle.rca as rca_module`.
    - Added test_rca_and_correlation_never_reference_scenario_metadata():
      asserts the tokens "scenario_name", "scenario_id", "expected_outcome",
      "test_instruction" never appear in the source of rca.py or
      correlation.py.

Tests:
  pytest tests/test_kubernetes_client.py tests/test_correlation.py tests/test_rca.py -q
  -> 76 passed (includes the new scenario-isolation guard test; no
     pre-existing test needed to change, confirming no behavioral change).
```

## Phase 3

```text
Implemented:
The existing investigation model (app/lifecycle/investigation.py's
investigate()) already performs bounded, read-only, parallel evidence
collection across Prometheus metrics, Loki logs, Kubernetes state
(deployment/pods/events/replicasets/init-containers), and the service's own
health endpoint, via asyncio.gather(..., return_exceptions=True) so a
failure in one collector never blocks the others and is recorded as a real
error rather than silently treated as "no data". Per-collector granular
audit events (investigation_started, evidence_prometheus, evidence_loki,
evidence_kubernetes, evidence_init_container) were already emitted to the
event bus (visible on Sentinel Live) by orchestrator.py's _investigate(),
each carrying the real collected values — this was independently verified
by reading orchestrator.py directly, correcting an earlier, less complete
grep-based read.

Per the requester's explicit decision, this phase does NOT introduce an
adaptive, hypothesis-driven, multi-round LLM tool-calling loop over named
tool contracts (get_metrics/query_logs/inspect_pods/etc., as sketched in
the brief). The brief's own §19 explicitly allows this: "Do not introduce
unnecessary agent complexity if deterministic investigation orchestration
is more reliable." Adding a second, LLM-driven investigation path alongside
the existing one would increase the attack surface for non-determinism
(different evidence on different runs of the same incident) without fixing
any concrete bug, and was rejected on that basis.

Gap found and closed:
  - The brief asks for named, bounded investigation budgets
    (MAX_INVESTIGATION_ROUNDS, MAX_INVESTIGATION_TOOL_CALLS,
    MAX_TOOL_CALLS_PER_ROUND, TOOL_TIMEOUT, MAX_LOG_LINES, MAX_METRIC_RANGE,
    MAX_RESPONSE_SIZE). The underlying bounds already existed in the
    codebase (per-client timeouts, LOOKBACK_SECONDS = 900, log-line caps in
    the Loki client, etc.) but were distributed as unlabeled default
    parameters with no single named constant a reviewer could point to and
    no explicit statement of the "1 round" design choice. This was a real,
    if purely documentation/naming, gap.

Files changed:
  app/lifecycle/investigation.py
    - Added a documentation block directly below LOOKBACK_SECONDS = 900
      mapping each of the brief's named tool concepts to the real existing
      method/client that already performs that role and its real bound
      (e.g. "get_metrics" -> PrometheusClient queries, bounded by
      LOOKBACK_SECONDS and per-call timeout; "query_logs" -> LokiClient,
      bounded by its max-lines setting; "inspect_pods"/"inspect_deployment"/
      "inspect_events"/"inspect_recent_deployment"/"inspect_previous_revision"
      -> KubernetesClient calls already used by investigate()/correlation.py).
    - Added `MAX_INVESTIGATION_ROUNDS = 1`, with a docstring stating this is
      a deliberate design choice (single-round parallel collection, not
      adaptive multi-round tool-calling) so a future adaptive investigator
      has an explicit, named switch to change rather than a hidden
      assumption baked into control flow.

Tests:
  pytest tests/test_investigation.py -q
  -> 9 passed (all pre-existing tests continue to pass unmodified; the
     change is additive documentation/constants only, so no new test was
     required to prove non-regression beyond the existing suite, which was
     re-run in full below).
```

## Phase 4

```text
Implemented:
Layered recovery validation already existed in app/lifecycle/validation.py:
RecoveryValidator polls settle -> timeout with independent, pure check
functions for Kubernetes state (check_replicas, check_pods_ready),
application health (check_health, which parses the health endpoint's JSON
body and distinguishes "degraded" from "ready"/"ok"/"not_ready" rather than
trusting HTTP 200 alone), metrics (check_error_rate, check_latency,
check_cpu, check_memory), each returning a real pass/fail/skip detail
string. A genuine before/after comparison for error rate already existed,
including a deliberate "recovering" allowance (ERROR_RATE_RECOVERY_FRACTION
= 0.4) that accounts for Prometheus's 5-minute rate() window lag rather
than fabricating an instant transition. Validation is incident-specific
(different active checks depending on which evidence is available for that
incident, not one universal validator), and the bounded
remediate/validate/re-investigate loop already existed in orchestrator.py
with two independent caps (max_actions_per_incident, and a separate hard
ceiling on re-investigation cycles) plus reopen_count-based retry limiting
and cross-incident cooldowns.

Gap found and closed:
  - Only error_rate had a real before/after ("X% -> Y%") presentation
    matching the brief's example format. Latency, CPU, memory, and replica
    counts only reported the "after" value, with no before/after framing,
    even though the "before" data (the incident's own Evidence, collected
    at investigation time) already existed and was already in scope at the
    orchestrator call site. This was a real, narrow gap: the brief's
    example output format ("p95: 1840ms -> 210ms PASS", "Ready replicas:
    1/2 -> 2/2 PASS") was not met for these fields, and — critically —
    the fix had to guarantee no fabricated values: a check with no
    baseline available must keep reporting only the "after" value, never
    invent a "before".

Files changed:
  app/lifecycle/validation.py
    - check_latency(current, threshold, baseline=None), check_cpu(...,
      baseline=None), check_memory(..., baseline=None): new optional
      trailing parameter. When baseline is None (not captured, or the
      incident has no such evidence), behavior and detail strings are
      byte-for-byte unchanged from before this change. When baseline is
      provided, the detail string is reframed as
      "<before> -> <after> (limit <threshold>)" using the real captured
      value, never a placeholder or estimate.
    - check_replicas(deployment, baseline_deployment=None): same pattern,
      producing e.g. "1/2 -> 2/2 replicas available" only when both a
      current and baseline deployment dict with available/desired replica
      counts are present; otherwise falls back to the original
      "{available}/{desired} replicas available" wording unchanged.
    - RecoveryValidator.validate() and RecoveryValidator._single_pass():
      signatures extended with baseline_p95_latency_seconds,
      baseline_cpu_cores, baseline_memory_bytes, and
      baseline_deployment (all default None), threaded through to the
      four check functions above.

  app/lifecycle/orchestrator.py
    - Both call sites of self.ctx.validator.validate(...) (the primary
      remediation path and the nested re-remediation path inside the
      bounded re-investigation loop) now also pass
      baseline_p95_latency_seconds=evidence.p95_latency_seconds,
      baseline_cpu_cores=evidence.cpu_cores,
      baseline_memory_bytes=evidence.memory_bytes, and
      baseline_deployment=evidence.deployment — all values already
      collected as part of that incident's real Evidence, never invented
      for this purpose.

  tests/test_validation.py
    - Added test_before_after_baseline_shown_when_captured_never_fabricated():
      asserts a baseline is reflected in the detail string when supplied
      (latency, cpu, memory, replicas), asserts the detail string contains
      NO "->" framing when no baseline is supplied (proving nothing is
      fabricated when the "before" isn't available), and asserts a
      failing-after-value case still reports correctly with a baseline.

  tests/engine_harness.py (test infrastructure, not production code)
    - FakeValidator.validate() signature extended with the same four new
      optional keyword arguments so it accepts calls in the same shape the
      orchestrator now makes; the fake still returns its scripted outcome
      unchanged (see "Test Results" below — this fix was required to keep
      8 pre-existing engine-level tests passing; see Remaining Issues for
      how this regression was found and fixed).

  tests/test_authorizations.py
    - The test's local monkeypatched _fake_validate() closure was given
      the same signature extension, for the same reason.

Tests:
  pytest tests/test_validation.py -q
  -> 27 passed (includes the new before/after test; every pre-existing
     test in this file passes unmodified, confirming the new parameters
     are purely additive).
  pytest tests/test_incident_engine.py tests/test_authorizations.py -q
  -> 32 passed (these are the tests that exercise the real orchestrator
     call sites end-to-end through FakeValidator/the monkeypatched
     validator; see Remaining Issues for the regression this surfaced and
     how it was fixed).
```

## Rollback Safety

Report:

```text
Target selection:
correlation.py's _find_valid_rollback_candidate() filters ReplicaSet
history to candidates that are: (1) for the same Deployment, (2) an older
revision than the one currently active, and (3) created before the
incident's onset timestamp — so a rollback can never "advance" to a
revision that postdates the incident, and never targets an unrelated
deployment.

Image validation:
kubernetes_client.py's _assert_valid_container_images() validates every
image reference (main containers AND init containers) for both the
current live spec and any candidate rollback target before it is ever
patched onto the cluster.

Placeholder protection:
_looks_like_a_valid_image_reference() rejects unsubstituted CI/CD template
placeholders (ACCOUNT_ID, REGION, PLACEHOLDER) by splitting the image
reference into exact path segments (re.split on "./:@") and comparing
whole segments — never a substring/regex match against the full string —
so a real, legitimate image containing one of those words as part of a
longer token (e.g. a region name embedded in a real registry host) is not
falsely rejected. This directly addresses the real historical incident
described in the brief (§6): Sentinel had previously rolled back to a
ReplicaSet still carrying
"ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/sentinel-sre-demo/citizen-service:PLACEHOLDER",
causing InvalidImageName failures in Kubernetes.

Init-container validation:
The same validation function is applied to spec.initContainers, not just
spec.containers — a deployment whose main containers look valid but whose
init container still carries a placeholder image is rejected in exactly
the same way, before any patch is attempted.

Audit trail:
orchestrator.py emits a structured "rollback_target_selected" event (real
revision number, real image list, real reason) before the Deployment patch
call, and correlation.py tracks rollback_candidates_skipped so a reviewer
can see which candidates were considered and rejected, and why, for every
incident — not just the one ultimately chosen.

This entire set of controls, and the associated failure modes (placeholder
images, init-container-only failures, digest-pinned images being accepted
correctly, no-valid-target, multiple-consecutive-invalid-candidates,
zero-replica-but-valid-template edge cases, and backward compatibility with
ReplicaSet data missing the images_valid key entirely), was already covered
by an extensive pre-existing test suite in test_kubernetes_client.py and
test_correlation.py before this phase began. No gaps were found here beyond
the scenario-isolation guard test added under Phase 2 above.
```

## Live Test A — Bad Deployment

```text
NOT PERFORMED IN THIS SESSION. This session had no reachable kubeconfig, no
kubectl/aws/terraform binaries, and no populated .env/API keys for
sentinel-ai (only .env.example templates) — the real AWS-hosted K3s cluster
described in chaos_scenarios.py (triggered via AWS SSM running
scripts/incident-scenarios.sh on the cluster's EC2 node) was not reachable
from here. Per the requester's explicit direction, this is stated plainly
rather than inferred or fabricated.

What can be said about the code path without live evidence:
Detection: a bad-deployment chaos scenario (or a real bad deployment) is
  expected to surface through Prometheus error-rate/latency alerts and/or
  Kubernetes event/pod-status signals feeding the existing detection path.
Investigation: investigate() collects deployment/pod/event/init-container
  state, recent metrics, and recent logs in parallel (Phase 3, unchanged
  behavior, hardened documentation only).
RCA: rca.py's cascade recognizes bad-deployment signatures (e.g. a recent
  deploy correlated with a spike in errors, or a crash-looping/failing
  init container) ahead of more generic rules, per its most-specific-first
  ordering (verified by reading rca.py; not exercised live here).
Decision / Policy / Rollback target / Action / Validation / Final state:
  would flow through the real, unpatched production code paths described
  above (confidence threshold >= 0.95 for rollback, deterministic policy
  validation, the rollback-target-safety controls under "Rollback Safety"
  above, then RecoveryValidator with the new before/after reporting) — but
  none of this was exercised against a live cluster in this session.
```

## Live Test B — Memory

```text
NOT PERFORMED IN THIS SESSION, for the same reason as Live Test A.

What can be said about the code path without live evidence:
Detection/Investigation: a progressive memory leak is expected to be
  distinguished from a temporary spike by rca.py's memory-leak rule, which
  (per reading the rule, not live execution) looks for sustained growth
  across the evidence window rather than a single elevated sample.
RCA/Decision/Action: for a genuine sustained leak, the expected action is a
  pod/deployment restart (confidence threshold >= 0.90) rather than a
  rollback, since no bad deployment is implicated.
Validation: check_memory's new before/after reporting (Phase 4 above) would
  show the real captured pre-action memory value against the real
  post-action value, or report memory as unavailable if either could not be
  captured — never a fabricated pair.
Final state: not observed live.
```

## Live Test C — Database

```text
NOT PERFORMED IN THIS SESSION, for the same reason as Live Test A.

What can be said about the code path without live evidence:
Detection/Investigation: a dependency/database outage is expected to
  surface via the health-check/dependency-check path and downstream
  error-rate impact on the citizen-facing service.
RCA: rca.py's downstream-dependency rule is expected to attribute the
  incident to the dependency rather than the citizen-service deployment
  itself.
Policy / Protected resource: PostgreSQL and other declared dependencies are
  on Sentinel's deny-list; policy is expected to block any autonomous
  mutation of the database (no restart/rollback/scale action is ever
  generated against a protected resource) and to allow only observation.
Action: none — this is the one path where the correct and only allowed
  autonomous behavior is to NOT act.
Escalation: expected to escalate to a human operator with the evidence
  gathered, rather than attempt any autonomous fix.
Final state: not observed live; the deny-list and escalation-on-blocked-
  policy logic were read in code but not exercised against a live outage.
```

## Test Results

Provide exact commands and results.

```text
Environment note: sentinel-ai's dependencies are not installed on the
device that hosts the repository, and running pytest directly against the
FUSE-mounted repo path was avoided (established risk from earlier work on
this codebase). The source tree (excluding __pycache__/*.pyc/.venv) was
therefore tarred, staged into an isolated sandbox, extracted onto real
(non-FUSE) disk, installed into a fresh virtualenv, and tested there. All
edits described above were made directly on the real repository via
targeted, assertion-guarded find/replace scripts (never blind overwrites),
then mirrored into the sandbox copy for testing.

$ cd sentinel-ai-test && .venv/bin/python -m pytest -q
476 passed, 2 warnings in 86.60s (0:01:26)

Targeted sub-runs (breakdown by area):

$ pytest tests/test_kubernetes_client.py tests/test_correlation.py tests/test_rca.py -q
76 passed in 0.08s

$ pytest tests/test_investigation.py -q
9 passed in 0.07s

$ pytest tests/test_validation.py -q
27 passed in 0.08s

$ pytest tests/test_incident_engine.py tests/test_authorizations.py -q
32 passed, 2 warnings in 25.01s

The 2 warnings in both full-suite and targeted runs are pre-existing,
unrelated deprecation warnings (anyio.abc.BlockingPortal alias, and
passlib's use of the deprecated stdlib `crypt` module) — not related to
this work.
```

## Remaining Issues

```text
Clearly identify anything that could not be tested. Do not claim something
is working if it was only inferred.

1. Live Tests A, B, C (bad deployment, memory leak, database outage)
   against the real AWS-hosted K3s cluster were NOT performed in this
   session, per explicit agreement with the requester (no reachable
   kubeconfig/kubectl/aws/terraform, no populated API keys for this
   session). Everything under the three "Live Test" sections above is a
   description of what the existing, unit/integration-tested code is
   expected to do, not a report of observed live behavior. This should be
   run and verified against the real cluster before treating Phase 2-4 as
   fully done per the brief's own Definition of Done (§47), which requires
   live evidence.

2. A genuine regression was introduced and then fixed during this phase:
   adding new optional baseline_* keyword arguments to the real
   RecoveryValidator.validate() (Phase 4) broke two test doubles that
   mirror its old signature (FakeValidator in tests/engine_harness.py, and
   an inline monkeypatched closure in tests/test_authorizations.py),
   causing 8 tests to fail with a TypeError that the orchestrator's
   generic internal-error handling silently converted into incident
   escalation instead of resolution. This was caught by running the full
   suite (not skipped or worked around), root-caused to the exact
   signature mismatch, and fixed by extending both test doubles with the
   same new optional parameters. The full suite is green (476 passed) as
   of the "Test Results" section above. This is flagged here rather than
   silently folded into "Tests: all passed" because it is a real example
   of exactly the kind of test-double drift that can mask a genuine bug
   if not caught by a full-suite run — worth being aware of if more
   optional parameters are added to validate() in the future.

3. No performance/cost measurement (brief §43) was performed against a
   live cluster, for the same access-limitation reason as (1). The
   existing bounds (LOOKBACK_SECONDS, log-line caps, per-client timeouts,
   now named/documented under Phase 3) were reviewed by reading the code,
   not measured under live load.

4. This report and the underlying gap analysis were produced by reading
   the repository's actual source (not assuming the brief's framing was
   accurate) and running the actual test suite (not assuming tests would
   pass). Where a claim above says something "was already implemented and
   tested," that is based on locating and reading the specific
   implementation and its corresponding test file(s), not on the brief's
   own description of what should exist.
```
