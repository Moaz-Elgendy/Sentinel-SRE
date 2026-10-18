# Sentinel SRE — Differentiation Upgrade Report

**Scope note (read first):** This work implements all five differentiators from
the differentiation-upgrade brief, staged in sequence exactly as agreed before
implementation began:

1. **Scope:** all five differentiators — Causal Incident Graph, Blast Radius /
   Risk-Aware Remediation, Operational Memory + Incident Fingerprinting,
   Incident Replay + What-If Simulation, and Sentinel Agent Evaluation — each
   with its own tests before moving to the next.
2. **GUI depth:** every differentiator is integrated into the *existing*
   incident page and its existing components (`LiveFlowDiagram`,
   `DiagnosisSection`, `EvidencePanel`, `PerformancePage`,
   `IncidentDetailPage`'s tab set) rather than introduced as new, disconnected
   pages or navigation.
3. **Live testing:** implemented and tested against the automated suite only.
   **Live-cluster verification was NOT performed.** This session has no
   reachable kubeconfig, `kubectl`/`aws`/`terraform` binaries, or populated
   AWS credentials for the real K3s cluster or chaos-scenario SSM commands —
   only `.env.example` templates exist. Every claim below about behavior is
   backed by an automated test that exercises the real code path with
   synthetic or hand-crafted data, never by an observation of the live
   cluster. Anywhere this report says a metric or scenario "would" report a
   given value, that is what the code computes given the inputs the test
   supplies — not a live measurement.

No structural rewrite was needed or performed. The existing lifecycle
pipeline (detect → correlate → investigate → RCA → policy → remediate →
validate → [re-investigate|escalate|resolve]) is unchanged; every existing
safety control (allow-lists, deny-lists, confidence thresholds, DRY_RUN,
RBAC scoping, audit logging, cooldowns, per-target concurrency isolation) is
preserved verbatim. All five differentiators are additive: new modules that
*read* the existing `Incident`/`Evidence`/`AttemptRecord` schema and either
project it into a new view (causal graph, replay), annotate a decision
already being made (risk), recall a fact already stored (memory), or score
an outcome against a known answer (evaluation) — none of them sit on the
remediation hot path, and none of them can execute an action or reach live
infrastructure. This last point is enforced structurally, not just by
intent: `replay.py` and `evaluation.py` import nothing from
`remediation.py`/`investigation.py`/`validation.py`'s `validate()`, so
neither module can touch a cluster even if asked to — a `grep` confirms it.

---

## Stage 1 — Causal Incident Graph

**Backend:** `app/lifecycle/causal_graph.py` (338 lines) projects one
already-persisted `Incident` into a labeled graph, computed on request rather
than maintained as a second live store. Every edge carries a `kind` that is
never blurred: `fact` (directly observed — a commit sha, a metric value),
`correlation` (co-occurrence, never causation — matching
`correlation.py`'s own stance), `hypothesis` (Sentinel's belief plus its
existing, already-concise reasoning text — never raw chain-of-thought),
`action` (a remediation candidate, allowed or denied with the real reason),
and `outcome` (what actually happened). No graph database was introduced;
at this incident volume a projection function a reviewer can read top to
bottom was judged more maintainable than a persisted store, an ingestion
pipeline, and a query language to keep in sync with the `Incident` schema.

**API:** `GET /api/incidents/{id}/causal-graph`.

**Tests:** `tests/test_causal_graph.py`, 7 tests.

**GUI:** `sentinel-gui/src/components/incident/CausalGraphTab.jsx`, wired as
the "Causal graph" tab on the existing incident detail page (no new page or
navigation entry).

---

## Stage 2 — Blast Radius / Risk-Aware Remediation

**Backend:** `app/lifecycle/risk.py` (201 lines) sits between the Decision
Engine and the Policy Engine and answers, for each candidate action, the
brief's specific questions: affected namespace/app, blast-radius scope
(`single_workload` vs. wider), reversibility, whether recovery validation is
available, and whether a less-impactful untried option exists. `level`
(low/moderate/high) is a closed, deterministic three-value classification
from a fixed table — never a float, never an LLM opinion — computed
purely to make facts the codebase already had explicit and visible, not to
add a second gate: Policy still enforces its own thresholds exactly as
before. `orchestrator.py` computes this assessment for every candidate and
attaches it to `PolicyVerdict.risk`, so it travels with the incident record
that already reaches the GUI — no new endpoint was needed.

**Tests:** `tests/test_risk.py`, 10 tests.

**GUI:** surfaced as a compact risk line (level, blast-radius scope,
reversibility, validation availability) on each action node in
`CausalGraphTab.jsx`, and reused verbatim in Stage 4's replay candidate
cards — one place renders risk, both consumers use it.

---

## Stage 3 — Operational Memory + Incident Fingerprinting

**Backend:** `app/lifecycle/memory.py` (207 lines, pre-existing from an
earlier phase) fingerprints incidents and recalls similar past ones at
investigation time. This phase found and fixed a genuine correctness bug
while wiring the GUI-facing distinction the brief calls for:
`SimilarIncident.as_supporting_note()` previously collapsed every
non-escalated outcome into "resolved autonomously," silently mislabeling a
past incident where the remediation attempt actually failed. It now
distinguishes escalated / no-attempt / execution-failed /
validation-unconfirmed / succeeded outcomes explicitly.

**Tests:** `tests/test_memory.py`, 10 tests (3 added this phase covering the
fix: a failed remediation must not read as "resolved," an unconfirmed
recovery must be flagged distinctly, and a past incident with no
remediation attempt at all must say so honestly).

**GUI:** `DiagnosisSection.jsx` (confirmed, via the import graph, to be the
live component — `ReasoningPanel.jsx` was dead code, referenced nowhere but
a stale comment) splits rule-based supporting signals from memory-based
"seen before" notes and tone-codes the latter (bad/warn/ok/neutral) from the
now-fixed backend phrasing.

---

## Stage 4 — Incident Replay + What-If Simulation

**Backend:** `app/lifecycle/replay.py` (332 lines) answers "what would
Sentinel's *current* rules conclude from this incident's recorded
evidence" — a recorded-event replay, not a full simulation engine, chosen
deliberately after establishing which pipeline stages are pure functions of
explicit inputs (`correlation.correlate()`, `rca.analyse()`,
`decision.DecisionEngine.candidates()`, `risk.assess_risk()`,
`policy.PolicyEngine.evaluate()`) versus live/stateful
(`investigation.py`'s async evidence collection, which must never be
re-run). Replay re-runs exactly the pure stages against the incident's
*stored* evidence, pinned to the incident's own `created_at` as the
reference clock rather than wall-clock time — an incident replayed a year
later must not have its deployment-recency check silently go stale and
flip `deploy_correlates_with_onset` to `False`. An `from_scratch` flag lets
a reviewer ask "what if we ignored what was already tried" by reconsidering
already-attempted actions. Replay never mutates the incident it reads and
never executes anything — the import graph makes this structural, not just
documented.

**API:** `GET /api/incidents/{id}/replay?from_scratch=<bool>`.

**Tests:** `tests/test_replay.py`, 8 module tests (no-evidence empty result,
bad-deployment recommends and allows rollback, reference-clock pinning,
denylisted-target denial, `from_scratch` reconsideration, root-cause-drift
flagging, unknown-root-cause escalation, and a non-mutation guarantee) plus
3 endpoint tests in `tests/test_gui_api.py` (auth required, 404 on an
unknown incident, and a full-app smoke test firing a real alert and hitting
the endpoint with both `from_scratch` values).

**GUI:** `ReplayTab.jsx`, wired as the "Replay" tab on the incident detail
page: recomputed diagnosis with a root-cause-drift warning banner,
per-candidate cards (action, allowed/denied, "would execute," risk line,
denial reason, rationale), a "reconsider already-tried actions" checkbox,
and a manual re-run control.

---

## Stage 5 — Sentinel Agent Evaluation

**Backend, ground-truth scoring:** `app/lifecycle/evaluation.py` (233
lines) measures decision quality against a *known* right answer — the only
source of one in this system is a deliberately injected chaos scenario.
`chaos_scenarios.py`'s `SCENARIOS` table now carries
`expected_root_cause`/`expected_action`/`expected_app` for the 7 of 9 named
scenarios where the RCA rule-precedence order (`rca.py`) produces exactly
one unambiguous outcome:

| scenario | expected root cause | expected first action |
|---|---|---|
| db-outage | chaos_database_fault | reset_chaos_fault |
| http-errors | chaos_http_fault | reset_chaos_fault |
| latency | chaos_latency_fault | reset_chaos_fault |
| notification-degradation | chaos_notification_fault | reset_chaos_fault |
| memory-leak | memory_leak | restart_deployment |
| full-outage | service_down | restart_deployment |
| bad-deployment | bad_deployment | *(none — root cause only)* |
| high-cpu | *(none — genuinely ambiguous)* | |
| crashloop | *(none — genuinely ambiguous)* | |

`high-cpu` and `crashloop` are deliberately left without an asserted ground
truth: `high-cpu` is ambiguous between `CPU_SATURATION` and
`CAPACITY_SHORTFALL` depending on incidental latency, and `crashloop`
plausibly rules-classifies as `BAD_DEPLOYMENT` (checked earlier in
precedence) rather than `POD_CRASH_LOOP` — guessing either would make a
"we don't know" scenario silently score as a wrong answer, which
`evaluation.py` is built never to do (`root_cause_correct` and
`decision_matched_expected` stay `None`, not `False`, whenever there is no
ground truth to compare against).

Recording and resolving are deliberately decoupled: `create_run()` only
records the expectation and a trigger timestamp — no AWS access, fully unit
testable — because triggering a scenario is a live AWS SSM operation this
session cannot perform. `resolve_run()` is called lazily at *read* time
(the same "derive from the store, don't invent a second live process"
pattern as `causal_graph.py`/`memory.py`/`replay.py`), looks for the first
incident on the run's app created at/after the trigger time, and scores it
only once that incident reaches a terminal status — giving up honestly
(not "forever pending") if none appears within a generous 30-minute window.

**Backend, ungrounded metrics:** `app/routers/performance.py` was extended
with 7 metrics that need no known right answer and are computed from the
general incident population, chaos-triggered or not:
`first_action_success_rate` (validation-passed rate of only the *first*
executed attempt per incident, distinct from the pre-existing
`remediation_success_rate` which spans every attempt including fallbacks),
`execution_failure_rate` (executed but failed to even apply),
`ineffective_remediation_rate` (applied fine, but validation failed —
`ValidationOutcome.UNAVAILABLE` is excluded from this and from
`recovery_validation_success_rate`, because "could not check" is not "checked
and failed"), `recovery_validation_success_rate`, `policy_rejection_rate`
(of every candidate ever ruled on, executed or denied-only), `escalation_rate`,
and `avg_investigation_latency_seconds` (detection → root-cause-analysis
timeline events — independently measurable, unlike the pre-existing, still
honestly-`null` `avg_time_to_detection_seconds`, which stays `null` for the
same pre-established reason as before: Sentinel is alerted by Alertmanager,
push-based, so it has no independent detection-latency measurement to
report). Every one of the 7 new rates returns `null` with an
`*_unavailable_reason` string when its denominator is empty, matching the
honest-null convention already established elsewhere in this file — never a
fabricated `0.0`/`1.0`.

**API:** `POST /api/evaluation/runs`, `GET /api/evaluation/runs`,
`GET /api/evaluation/summary` (new); `GET /api/performance/summary`
(extended, backward-compatible — existing fields unchanged, 7 new fields and
2 new `sample_size` entries added).

**Tests:** `tests/test_evaluation.py`, 12 module tests (pure
`create_run`/`resolve_run`/`aggregate_evaluation_metrics` coverage,
including that a scenario with no ground truth is never scored even once
resolved, and that root-cause-correctness and decision-accuracy are scored
on independent axes). `tests/test_gui_api.py` adds: 5 evaluation-endpoint
tests (auth required, unknown scenario is 404, `"all"` is rejected 422, a
full create→list→summary lifecycle, and a genuine end-to-end resolution
against a real incident) and 1 performance-extension test
(`test_performance_evaluation_metrics_distinguish_failure_modes`) that seeds
4 hand-crafted incidents directly into the store — denied-then-allowed
-and-passed, applied-but-validation-failed, execution-itself-failed, and
applied-with-validation-unavailable — and pins all 7 new rates plus 2 new
sample-size fields to exact expected values, to catch a regression in any
one failure-mode distinction rather than merely confirm "not null."

**GUI:** `PerformancePage.jsx` (existing component, extended in place, no
new page) gains a "Decision quality" metric group for the 7 new
`performance.py` rates, an added "Avg. investigation latency" metric in the
existing "Speed" group, and a new "Chaos-scenario evaluation" panel showing
the aggregate RCA-correctness/decision-accuracy rates alongside a table of
every recorded evaluation run (scenario, app, trigger time, resolution
status, and per-axis correct/incorrect/no-ground-truth verdicts, reusing the
existing `StatusBadge` tone convention rather than inventing a new color
scheme). New supporting files: `src/api/evaluation.js`,
`src/hooks/useEvaluation.js`.

---

## Test Results

**Backend (`sentinel-ai`):** full suite, 534 passed, 0 failed, 0 skipped —
run twice in this final phase: once immediately after the newest test
(`test_performance_evaluation_metrics_distinguish_failure_modes`) was added,
to confirm its hand-computed expected values were correct on the first real
run, and once more as the full-suite regression check before writing this
report. Zero regressions from any of the five stages' work.

**Frontend (`sentinel-gui`):** `oxlint` across the full source tree reports
0 warnings in every file touched by this work (new: `CausalGraphTab.jsx`,
`ReplayTab.jsx`, `api/evaluation.js`, `hooks/useEvaluation.js`,
`PerformancePage.jsx`; modified: `DiagnosisSection.jsx`,
`IncidentDetailPage.jsx`, `api/incidents.js`) — 2 pre-existing warnings
remain in `AuthContext.jsx` and `useLiveIncident.js`, both untouched by this
work and present before it. `vite build` (fresh `npm ci` install, since the
on-disk `node_modules` in the working copy was missing its platform
`esbuild` binary — an unrelated, pre-existing local install issue, not a
regression from this work) completes cleanly: 2758 modules transformed, no
errors, `PerformancePage` and every new/changed component bundle into their
own chunks as expected.

---

## Known Limitations / Explicit Disclosures

1. **Live-cluster verification was not performed**, for any of the five
   stages, for the reason stated at the top of this report: no reachable
   kubeconfig, `kubectl`/`aws`/`terraform`, or populated credentials in this
   session. Every behavioral claim in this report is backed by an automated
   test against synthetic or hand-crafted data, not an observed live run.
   This should be verified against the real cluster and the real chaos-SSM
   surface before treating any of the five differentiators as fully done
   against the brief's own definition of done, which asks for live evidence.
2. **Two chaos scenarios (`high-cpu`, `crashloop`) carry no evaluation
   ground truth**, by deliberate design — see Stage 5 above — not an
   oversight. `evaluation.py` will never score them, which is the correct
   behavior for a genuinely ambiguous scenario, but it also means Stage 5's
   ground-truth metrics can only ever reflect 7 of the 9 named scenarios.
3. **`avg_time_to_detection_seconds` and `temporary_overrides` remain
   `null`** — both are pre-existing, deliberate decisions from before this
   phase (Alertmanager's push-based alerting gives Sentinel no independent
   detection-latency signal to measure; temporary SRE authorization is a
   later implementation phase), preserved rather than overridden, and
   unrelated to any of the five differentiators.
4. **No new database migration tooling was introduced.** The
   `evaluation_runs` table was added to the existing `_SCHEMA` dict in
   `sqlite_store.py`, matching how every other table in this store is
   defined — new deployments get it for free; no existing deployment has
   data to migrate, since this is new functionality.
5. This report was produced by reading the actual source of every module
   discussed and running the actual test suite and build, not by assuming
   either would pass — the two full-suite runs and the two independent lint
   and build passes described above are the primary evidence for every
   "tests: N passed" claim in this document.
