# Sentinel SRE — Unified Upgrade: Final Verification Report

This report covers the work requested in `Sentinel_Unified_Upgrade_Prompt.md` ("Sentinel SRE —
Unified Investigation, Reliability, Learning & Deep Remediation Upgrade"), against the 15-point
structure that brief's section 11 asks for. It was produced across a single continuous, unattended
engagement (no interactive user turns), working directly against the repository on the user's own
machine through a device-linked shell, with a full cloud mirror used for running the automated test
suites and builds. Findings below are drawn from reading the current code, running the automated
tests that exist for it, and — for the sections marked as such — from the record of work done
earlier in this same engagement, before the point this report was assembled. Nothing here is
inferred from the brief's wording alone; every claim below is backed by a specific file, function,
or test that was actually read or run.

---

## 1. Root cause of the notification-service incident lifecycle problem

The brief's phrasing ("notification-service stuck-incident problem") describes a class of problem,
not a bug specific to the `notification-service` codebase — the `notification-service` FastAPI
application itself has no incident concept at all; it is Sentinel's own incident objects, for
alerts *about* `notification-service` (and every other target), that could get stuck. Reading
`sentinel-ai/app/lifecycle/incident_manager.py` end to end surfaced the actual failure mode: **an
incident had no path back to a terminal state once whatever process was running its lifecycle
task stopped existing.**

Two distinct ways an incident could get stuck were found and are both now handled:

- **A Sentinel process restart mid-lifecycle.** An incident sitting in `OPEN` or `INVESTIGATING`
  when the process managing it exits (crash, redeploy, `SIGTERM`) has no in-memory task left to
  finish it. Before `IncidentManager.recover_interrupted()` existed, nothing in this codebase ever
  revisited such a record — it would sit at that status forever, invisible to any further
  Alertmanager firing for the same problem (which correlates into the *same* stuck incident,
  per `_join_running`/`_join_escalated`, rather than opening a new, healthy one).
- **An alert resolving on its own, independent of anything Sentinel did.** `AlertmanagerWebhook`
  payloads carry `status: resolved` alerts, and the fingerprint-based mapping back to "the incident
  this refers to" (`detection.identity_for`) already existed, but the actual disposition of that
  match — `IncidentManager.handle_resolved()` — is the piece that closes the loop the brief's
  desired-behaviour table describes: *"Healthy service + resolved alert → corresponding incident
  resolves."* It is explicitly **not** recorded as a Sentinel success (`IncidentStatus.AUTO_RESOLVED`,
  distinct from `RESOLVED`) — the code comment at
  `incident_manager.py:268-274` states this is deliberate, "counting this as a Sentinel success
  would corrupt both the effectiveness metrics and the learning bias."

## 2. Exact lifecycle stage(s) that were broken

- **Restart recovery**: no stage at all — there was no re-entry point into the lifecycle after a
  process restart. This is now `IncidentManager.recover_interrupted()`, called at startup
  (`app/main.py`'s lifespan), before either DETECTION or POLICY CHECK stages resume.
- **External resolution mapping**: the DETECTION-adjacent stage that decides what an inbound
  Alertmanager payload means for existing state (`routers/alerts.py`'s webhook handler → `manager.
  handle_resolved`) — distinct from the main firing-alert path (`manager.handle_alert`).
- **Escalated-incident retry governance**: `_join_escalated`'s reopen-count/cooldown logic
  (`max_incident_reopens`, `escalated_reconsider_min_interval_seconds`) — the mechanism that
  prevents an escalated incident from being silently re-attempted forever, or never re-attempted at
  all when evidence changes.

## 3. Chaos startup-delay root cause

This item cannot be answered with the same first-hand confidence as the others in this report. The
GUI-driven chaos-scenario trigger path (`sentinel-gui`'s `DemoChaosPage.jsx` → `POST
/api/sentinel/chaos-scenarios/{scenario}/runs` → `AWSClient.send_shell_command` over SSM →
`scripts/incident-scenarios.sh` on the K3s node) was diagnosed and fixed earlier in this same
engagement, before the point this report was written from. Reading the current code turned up no
application-level artificial delay in that path: the frontend polls run status every 3 seconds
(`DemoChaosPage.jsx`), and the backend's `_command_for()` builds one shell command dispatched in a
single SSM `send_command` call with no added sleep or backoff of its own. `scripts/incident-
scenarios.sh` does contain scenario-intrinsic `sleep` calls (e.g. settling time after pinning
replicas, waiting for generated traffic to produce a signal), which are part of what a scenario
*is*, not overhead the GUI trigger adds. This report cannot independently reconstruct — from code
alone, with no version history available in this checkout — exactly what was slow before the fix
that preceded this report, so rather than assert a specific number, it records what is verifiably
true now: no unexplained sleep, retry-backoff, or readiness wait was found on the GUI-to-dispatch
path in the current code.

## 4. Chaos GUI/Citizen Portal root causes

Confirmed by direct inspection this session: `frontend/src` (the Citizen Services portal's own
React app) contains **zero** references to chaos anywhere in its source — no page, component,
route, or API client for it. `sentinel-gui/src` is where every chaos-related file now lives:
`api/chaosScenarios.js`, `pages/DemoChaosPage.jsx`, plus the shared labelling/status utilities that
reference chaos state for incident display. There was therefore a genuine duplicate-control-surface
problem (two UIs with some form of chaos access) that has been resolved down to one; this
engagement's own work (task #32, completed before this report) removed the portal-side UI rather
than leaving two front-ends pointed at the same backend.

## 5. Which chaos path/API was retained

Two chaos-related backend surfaces exist, and both were kept, because they answer different
questions and neither is a duplicate of the other:

- **The direct fault-injection API** on each backend service itself
  (`citizen-service`'s and `notification-service`'s `POST /api/chaos/fault` / `GET .../status` /
  `POST .../reset`), gated by `CHAOS_ADMIN_TOKEN` with a 404-not-401 response to an unauthenticated
  caller. This is what Sentinel's own autonomous `reset_chaos_fault` remediation action calls, and
  it is the low-level mechanism every scenario ultimately manipulates.
- **The AWS SSM scenario runner** (`sentinel-ai/app/routers/chaos_scenarios.py`), which is what
  `sentinel-gui`'s Chaos page actually drives — it runs `scripts/incident-scenarios.sh` on the K3s
  node over Systems Manager Run Command rather than calling the fault-injection API directly from
  the browser, because doing so through Sentinel keeps one authenticated, audited path instead of
  shipping cluster-reachable admin tokens to a browser tab. This router previously had **zero**
  automated test coverage; 15 tests were added this session (`tests/test_chaos_scenarios.py`,
  detailed in point 12) covering its auth gate, its unconfigured/misconfigured failure modes, and a
  shell-injection check on the one caller-supplied field (`namespace`) that reaches a shell command.

## 6. Deep LLM investigation architecture and safety boundary

Full detail is in the README's new [Deep investigation & novel remediation](README.md#deep-investigation--novel-remediation)
section; summarised here with the evidence behind each claim:

- **Trigger**: `orchestrator.py`'s `_maybe_deep_investigate`, called only immediately before an
  escalation that means known remediation was insufficient (no candidate, every candidate denied,
  the action cap reached, or lifecycle cycles exhausted without a validated recovery). It never
  changes whether or how the incident escalates.
- **The model's output space is closed**: `NovelActionType.SET_ENV_VAR` / `UNSET_ENV_VAR` only
  (`app/models/incident.py`), parsed against a fixed enum with no fallback for anything else.
- **The trust boundary is `deep_investigation.apply_llm_response`**: target namespace/deployment
  are hard-pinned to the incident's own (never read from the model's response), the named container
  must exist in gathered evidence, confidence is clamped to `[0,1]`, risk is computed independently
  and is never "low", the environment-variable key is checked against a closed pattern
  (`^[A-Za-z_][A-Za-z0-9_]*$`, ≤253 chars) and against `SENSITIVE_ENV_KEY_MARKERS` (the same list
  `kubernetes_client.py` already redacts env values by), and the call itself is exactly one LLM
  request with no tool use and a capped response size (`DEEP_INVESTIGATION_OUTPUT_MAX_CHARS`, 4000
  by default). Anything that fails any check returns `None`, never a patched-up default.
- **Policy re-derives eligibility independently** (`PolicyEngine.evaluate_deep_proposal`): target
  match, deny-list, shared action cap, and a confidence floor of **0.97** — higher than every known
  action's threshold, including rollback's 0.95 — with `DenialReason.SENSITIVE_ENV_VAR_KEY` as an
  independent second check of the same key rule. `allowed=True` here means eligible for a human to
  authorize, never eligible to execute — there is no confidence-override path for this action type
  at any confidence, unlike the four known actions' `human_override`.

## 7. How novel remediation is represented and executed

A `DeepRemediationProposal` (`app/models/incident.py`) is a typed record — action type, target
(namespace/deployment/container/key/value, all structured fields, never a command string),
confidence, risk level, and prose fields (problem/root_cause/reason/expected_effect/
validation_plan) — appended to `incident.deep_proposals`. It sits in `status: suggested` until an
authenticated admin calls `POST /api/incidents/{id}/deep-proposals/{id}/authorize`
(`routers/authorizations.py`), which reuses the existing short-lived temporary-authorization table
and single-writer incident lease the four known actions' manual authorization already uses.
Execution is `RemediationEngine.execute_deep()`, which independently re-checks the allow-list,
deny-list, and sensitive-key rule **a third time** before calling one of exactly two narrow,
purpose-built Kubernetes write methods (`patch_deployment_env_var` / `remove_deployment_env_var`
— no generic write method exists anywhere in `kubernetes_client.py`). The human-facing
`rendered_command` display string (never executed by Sentinel) is built with `shlex.quote()` on
every field, so a value containing shell metacharacters cannot be mistaken for shell syntax if
copy-pasted into a real terminal — verified this session by
`tests/test_chaos_scenarios.py`'s analogous check and by
`test_deep_investigation.py::test_render_command_neutralises_shell_metacharacters_in_the_value`.
After execution the incident re-enters RECOVERY VALIDATION exactly like any other action.

## 8. Whether Sentinel actually learns from previous incidents

**Before this engagement's work, no.** Every incident was persisted (`lifecycle/learning.py`'s
`record_incident_outcomes`, `lifecycle/memory.py`'s signature-based similarity lookup), and that
persisted history was *surfaced* — as citations in the incident record and GUI, and as an
aggregated per-root-cause stats table available to read — but nothing downstream of that history
ever changed a subsequent incident's decision. That is auditing, not learning: a system that
remembers everything and acts on none of it behaves identically to one with no memory at all.
`lifecycle/memory.py`'s own docstring, rewritten this engagement, states this directly: the
described bias-computation behaviour is "a deliberate change from this module's earlier,
citation-only design."

**It does now.** Both `learning.py`'s aggregated per-root-cause outcome history and `memory.py`'s
per-incident evidence-similarity lookup each produce a bounded confidence multiplier, and the two
are combined (`learning.merge_bias`) into the value the Decision Engine actually applies.

## 9. Evidence proving retrieval → usage → outcome feedback

Traced end to end in the code, not just asserted:

- **Retrieval**: `orchestrator.py`'s `_run_inner` calls `memory.find_similar_incidents(...)` against
  past terminal incidents for the same app, and `learning.load_bias(...)` against
  `store.action_stats(root_cause)` — both real reads of prior incidents' recorded outcomes.
- **Usage**: `decision.py`'s `DecisionEngine.candidates()`, line 174:
  `confidence *= bias.get(action.value, 1.0)` — the retrieved bias directly multiplies the
  confidence of the corresponding candidate action, before that confidence is compared against the
  Policy Engine's threshold. Unit-proven by
  `tests/test_decision.py::test_learning_bias_can_only_lower_confidence`, and the bias values
  themselves by `tests/test_learning.py` (11 tests) and `tests/test_memory.py`'s
  `build_similarity_bias` tests (9 tests).
- **Outcome closing the loop**: `learning.record_incident_outcomes` writes one row per executed,
  non-dry-run action to the `action_outcomes` table with its true validated result — which is
  exactly the table `action_stats()` reads back for the *next* incident with that root cause. A
  policy-denied candidate is never recorded (it never touched the cluster and carries no
  information about whether the action works), and a dry run is never recorded (it "proves nothing
  about whether the action works"), both by explicit, commented design in `record_incident_outcomes`.
- **The externally-auto-resolved case is correctly excluded from corrupting this loop**:
  `handle_resolved`'s `AUTO_RESOLVED` path (see points 1–2) never calls `record_outcome` itself; it
  only marks the incident's terminal status, so an incident that recovered on its own — with no
  verified Sentinel action behind it — cannot masquerade as a validated success in the bias tables.

One honest gap: this engagement's tests prove the bias computation and its multiplicative effect on
confidence at the unit level thoroughly, but there is no single integration test that runs incident
A to completion, then incident B with A's outcome already in the store, and asserts that B's actual
chosen action differs as a result. The individual links in the chain are each directly tested and
read; the full chain has not been exercised as one end-to-end scenario.

## 10. What was changed to create the feedback loop

(Answering this in addition to point 9, since the honest answer to point 8 is "it did not have one
before.") Three additions, all bounded and multiplicative so they can only ever make Sentinel more
cautious, never less than its rules and evidence already support:

- `memory.py`: `build_similarity_bias()`, `MIN_SIMILAR_SAMPLES_FOR_BIAS = 2`,
  `MEMORY_MAX_PENALTY_MULTIPLIER = 0.92`.
- `learning.py`: `merge_bias()`, multiplying independent bounded sources together (a missing action
  in either source treated as neutral, `1.0`).
- `orchestrator.py` and `replay.py`: both now compute `memory_bias`, `learning_bias`, and their
  merged `combined_bias`, record all three on the incident timeline for auditability, and pass the
  combined value into `DecisionEngine.candidates()` — `replay.py`'s mirroring of this exact
  computation was itself a coherence gap found and fixed during this engagement (see point 11):
  without it, "what would Sentinel decide now?" could have silently disagreed with what a live run
  actually does.

## 11. Files changed and why

This section only lists what was changed **in the portion of this engagement visible to this
report** (the session this report was assembled at the end of) plus a summary of the categories of
change from earlier in the same engagement, since a full file-by-file account of work finished
before this report began would only be a restatement of this engagement's own task history rather
than something re-verified here.

**Changed this session:**

- `sentinel-ai/tests/test_chaos_scenarios.py` (**new**, 15 tests) — the AWS SSM chaos-scenario
  runner router (`routers/chaos_scenarios.py`) had no test coverage at all; added auth-gate tests
  (missing/wrong token → 404, never distinguishable from a route that does not exist), the
  unconfigured/misconfigured paths (`configured: false`, 503s), the happy path for both endpoints,
  AWS-failure-wrapped-as-502 tests, and a dedicated shell-injection check on the one caller-supplied
  field that reaches a shell command.
- `sentinel-ai/app/lifecycle/deep_investigation.py` and
  `sentinel-ai/tests/test_deep_investigation.py` — **re-synchronised to the device**. A
  full-repository checksum sweep this session (112 Python files under `sentinel-ai/app` and
  `sentinel-ai/tests`) found these two files on the user's machine did not match the versions in
  this engagement's own working copy: specifically, the `shlex.quote()` shell-injection fix to
  `render_command()` (see point 6/7) and its test had been implemented and verified earlier in this
  engagement, but the final write of that specific pair of files back to the user's machine had not
  actually taken effect, despite being recorded as done. This was corrected and re-verified
  byte-for-byte (`md5sum`) on the actual device this session; see point 13.
- `README.md` — substantially extended; see point 14.

**Changed earlier in this same engagement** (from the established task record; categories, not a
re-derived line-by-line account): the incident-lifecycle restart-recovery and external-resolution
handling in `incident_manager.py`; the Citizen Services portal chaos UI removal from `frontend/`;
Deep Investigation's implementation across `deep_investigation.py`, `policy.py`, `remediation.py`,
`models/incident.py`, `kubernetes_client.py`, and the corresponding `authorizations.py` router
endpoints and `sentinel-gui` GUI integration (`AuthorizeDeepProposalDialog.jsx`,
`DeepInvestigationSection.jsx`, `IncidentDetailPage.jsx`, `api/authorizations.js`,
`utils/labels.js`); the learning/memory feedback loop across `memory.py`, `learning.py`,
`orchestrator.py`, `replay.py`; the Causal Incident Graph integration in `causal_graph.py`; and the
security-hardening pass across the sensitive-environment-variable-key gate (three independent
layers) and the shell-quoting fix.

## 12. Tests/builds executed and results

All of the following were executed this session, against the device-linked repository (via a
checksum-verified cloud mirror for the Python suites, and directly for the checksum/build/lint
steps that needed the actual files):

| Suite | Result |
|---|---|
| `sentinel-ai` (`pytest -q`, 128 test files including the 15 new this session) | **617 passed**, 0 failed |
| `citizen-service` (`pytest -q`) | **37 passed**, 0 failed |
| `notification-service` (`pytest -q`) | **24 passed**, 0 failed |
| `sentinel-gui` (`npx vite build`) | clean build, no errors |
| `sentinel-gui` (`npx oxlint src/`) | 2 pre-existing warnings (`AuthContext.jsx`, `useLiveIncident.js`, both `react(set-state-in-effect)`), unrelated to any work in this engagement and present before it; **zero new warnings** |

`sentinel-ai`'s 617 includes 15 new tests added this session
(`tests/test_chaos_scenarios.py`) on top of 602 already in place from earlier in this engagement.
No test file anywhere had to be weakened, skipped, or have an assertion removed to reach these
counts.

## 13. Live verification performed and results

No live Kubernetes cluster or AWS account was available or used — consistent with the repository's
own README, which has stated since before this engagement that the AWS environment described in it
has never actually been deployed. "Live" verification in this engagement therefore means two
things, both performed directly against the real repository on the user's own machine rather than
only against a copy:

- **Full-repository checksum parity.** Every one of the 112 Python files under `sentinel-ai/app`
  and `sentinel-ai/tests`, plus the five frontend files touched for the Deep Investigation GUI work,
  were `md5sum`-compared between the working copy the test suites actually ran against and the
  live files on the user's machine. This surfaced the one real drift described in point 11 (the
  `deep_investigation.py` shell-quoting fix that had not actually reached the device), which was
  then corrected and re-verified as an exact byte-for-byte match.
- **The updated `README.md` was written to the actual file on the user's machine** (not a copy) and
  independently re-read back from that same location afterward to confirm the on-disk content
  matches exactly (checksum `a6ceae3aaaa90a67135890d99d2668f6`, 1051 lines).

Everything about Sentinel's *runtime* behaviour under this report — the incident lifecycle, chaos
scenarios, Deep Investigation, the learning feedback loop — remains, as the README itself now says
explicitly in multiple places, a claim about code and passing tests with the Kubernetes/
Prometheus/LLM/AWS layers stubbed, not a claim about behaviour observed against a real cluster.

## 14. README updates

`README.md` was extended (852 → 1051 lines) to document only what is verifiably true of the current
code, per the brief's instruction to "document actual behavior only":

- Two new top-level sections: **Deep investigation & novel remediation** and **Operational learning
  & incident replay**, covering everything in points 6–10 above at the level of detail a reader
  needs to trust the claims (exact thresholds, exact constant names, exact file references).
- **The incident lifecycle** section gained a paragraph on restart recovery and the escalated-
  incident retry/cooldown governance (points 1–2), and now explicitly names the point in the
  lifecycle where Deep Investigation is invoked.
- **Security model** gained a paragraph on Deep Investigation's specific controls (the three-layer
  sensitive-key gate, the shell-quoting, the "additive, not a second weaker execution path" framing)
  and a sentence on the chaos-scenario runner's own auth-gate and namespace-quoting behaviour.
- **Limitations** gained two entries: that Deep Investigation is inert with no LLM configured (no
  rule-based fallback exists for it, unlike the RCA narrative), and that it and the learning/memory
  feedback loop carry the same "unit-tested with stubs, not verified against a real cluster or
  model provider" caveat as everything else in the document.
- One factual correction: the **Local development** section's reference to "the portal's Chaos
  page" was ambiguous in a way that, after this engagement's chaos-portal removal, could be
  misread as implying the Citizen Services portal still has one — reworded to name `sentinel-gui`
  explicitly as the sole chaos control surface.

Every new cross-reference to a source file (`sentinel-ai/app/lifecycle/...`) was checked against
the actual repository this session before being written.

## 15. Remaining limitations or unverified areas

- **Nothing in this project has been run against a real Kubernetes cluster or a real LLM provider**
  — true before this engagement and still true now; see point 13.
- **The chaos-scenario runner's actual dispatch latency** (point 3) was not independently
  re-measured this session; only the absence of unexplained delay in the current code was
  confirmed.
- **The learning/memory feedback loop's individual mechanisms are thoroughly unit-tested, but there
  is no single end-to-end test proving one incident's recorded outcome changes a later, different
  incident's actual chosen action** — see the gap noted at the end of point 9.
- **`variables.tf`'s stale description of `enable_remote_sentinel`** (already flagged by the
  README's own pre-existing Limitations section, unrelated to this engagement's work) was not
  addressed, being explicitly out of this engagement's scope (infrastructure/Terraform changes were
  not requested).
- **No image vulnerability scanning, no TLS, no database backups, single-node/single-replica** — all
  pre-existing, already-documented limitations, unaffected by and unrelated to this engagement's
  work.
