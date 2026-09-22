"""
INCIDENT REPLAY / WHAT-IF SIMULATION.

Answers "what would Sentinel decide?" for an incident that already
happened, using the evidence it already recorded — never by re-investigating
(no Prometheus/Loki/Kubernetes/GitHub calls) and never by touching the
cluster (no remediation, no health probes). This is a deliberate "recorded
event" replay, not a full simulation engine with a mocked cluster: every
stage this module calls (correlation.correlate, rca.analyse,
decision.candidates, risk.assess_risk, policy.evaluate) is already pure,
synchronous, and unit-tested in isolation, and replay's only job is to run
that same pipeline again against a stored `Evidence` snapshot instead of a
live one, plus surface every candidate's outcome rather than stopping at the
first one (see `CandidateReplay.would_execute`).

### Safety boundary — enforced by the import graph, not just this docstring

This module imports nothing from `app.lifecycle.remediation`,
`app.lifecycle.investigation`, or `app.lifecycle.validation`'s `validate()`
(only `RecoveryValidator.is_available_for()`'s *boolean result* is ever
passed in, computed by the caller — this module never holds a reference to
the validator, the Kubernetes client, or any other client capable of a
network call). There is therefore no code path in this module that can
reach a live system, let alone mutate one — the same "cannot get access it
doesn't have" pattern rca.py uses to bound the LLM.

### Why "recorded evidence", not "re-run investigation"

Re-investigating would ask Prometheus/Loki/Kubernetes for evidence *now*,
which describes the cluster's current state, not the state at the time of
the incident being replayed — that would not be a replay of the incident at
all, it would be a new investigation that happens to reuse an old alert.
Using the stored `Incident.evidence` is what makes this an honest replay.

### Why `incident.created_at` as the reference clock

`correlate()`'s deployment-onset window and `policy.evaluate()`'s cooldown
math are both relative to "now". Using the actual current time would make a
six-month-old incident's deployment look six months old instead of the two
minutes old it actually was when Sentinel first saw it — silently flipping
`deploy_correlates_with_onset` to False and changing the diagnosis. Pinning
`now` to `incident.created_at` (evidence collection happens in the same
orchestrator run, seconds later) reproduces the time-relative facts as they
actually were.

### Why current PolicyConfig and current learning bias, not historical ones

Unlike evidence, policy thresholds/deny-lists and the learning bias are not
versioned anywhere in this codebase (there is exactly one `PolicyConfig` in
force at a time, live-editable via the Administration Center), so there is
nothing to reconstruct even if it were desirable. It is also the more useful
question to ask: "if this evidence came in today, under today's tuned
policy and today's operational memory, what would Sentinel do?" — which is
exactly what an operator tuning thresholds or reviewing a policy change
wants replay to answer.

### `from_scratch`

By default, replay respects the incident's own recorded `attempts` (an
action already executed in this incident is excluded from the candidate
list, exactly like a live decision continuing from where the incident left
off). Passing `from_scratch=True` reconsiders the full action ladder as if
nothing had been tried yet — useful for "what if the very first action had
been different" rather than "what happens next from here".
"""
from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any

from app.lifecycle import correlation, learning, memory, rca, risk
from app.lifecycle.decision import DecisionEngine
from app.lifecycle.evidence_signature import compute_signature
from app.lifecycle.policy import PolicyContext, PolicyEngine
from app.models.incident import Incident


@dataclass
class CandidateReplay:
    """One candidate action's hypothetical outcome, in ladder order."""

    candidate_index: int
    action: str
    namespace: str | None
    deployment: str | None
    confidence: float
    rationale: str
    risk: dict[str, Any]
    allowed: bool
    denial_reason: str | None
    denial_detail: str
    # True for exactly one candidate: the first one, in order, that would be
    # allowed. That mirrors the real orchestrator loop, which executes the
    # first allowed candidate and never reaches the rest — everything after
    # it here is "what would have happened if this one had been denied too",
    # informational rather than a claim about what actually would run next.
    would_execute: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_index": self.candidate_index,
            "action": self.action,
            "namespace": self.namespace,
            "deployment": self.deployment,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "risk": self.risk,
            "allowed": self.allowed,
            "denial_reason": self.denial_reason,
            "denial_detail": self.denial_detail,
            "would_execute": self.would_execute,
        }


@dataclass
class ReplayResult:
    incident_id: str
    reference_time: float
    replayed_at: float
    from_scratch: bool
    hypothesis: dict[str, Any] | None
    recorded_root_cause: str | None
    hypothesis_root_cause_changed: bool
    learning_bias: dict[str, float]
    memory_bias: dict[str, float]
    candidates: list[CandidateReplay]
    chosen_action: str | None
    would_escalate: bool
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "reference_time": self.reference_time,
            "replayed_at": self.replayed_at,
            "from_scratch": self.from_scratch,
            "hypothesis": self.hypothesis,
            "recorded_root_cause": self.recorded_root_cause,
            "hypothesis_root_cause_changed": self.hypothesis_root_cause_changed,
            "learning_bias": self.learning_bias,
            "memory_bias": self.memory_bias,
            "candidates": [c.to_dict() for c in self.candidates],
            "chosen_action": self.chosen_action,
            "would_escalate": self.would_escalate,
            "notes": self.notes,
        }


def _empty_result(incident: Incident, *, from_scratch: bool, replayed_at: float, note: str) -> ReplayResult:
    recorded = incident.hypothesis
    return ReplayResult(
        incident_id=incident.id,
        reference_time=incident.created_at,
        replayed_at=replayed_at,
        from_scratch=from_scratch,
        hypothesis=None,
        recorded_root_cause=recorded.root_cause.value if recorded else None,
        hypothesis_root_cause_changed=False,
        learning_bias={},
        memory_bias={},
        candidates=[],
        chosen_action=None,
        would_escalate=True,
        notes=[note],
    )


def replay_incident(
    incident: Incident,
    *,
    policy: PolicyEngine,
    store: Any,
    min_replicas: int,
    max_replicas: int,
    cpu_threshold_cores: float,
    error_rate_threshold: float,
    p95_threshold_seconds: float,
    recovery_validation_available: bool,
    chaos_surface_available: bool,
    from_scratch: bool = False,
) -> ReplayResult:
    """Re-run RCA -> Decision -> Risk -> Policy against `incident`'s stored
    evidence. Never executes, never calls a live client. See module
    docstring for the full set of design decisions behind this.

    `store` needs only the read-only methods this already calls in
    production (`recent_executed_actions`, whatever `learning.load_bias`
    calls, and `list_terminal_incidents_for_app` for the same operational-
    memory similarity lookup the live orchestrator does) — the same
    `SQLiteStore` the orchestrator already holds. Both `learning_bias` and
    `memory_bias` read the store's CURRENT accumulated history, not a
    snapshot from when this incident actually ran. That is a deliberate,
    pre-existing choice (`learning_bias` already worked this way): this
    module answers "what would Sentinel decide about this evidence, using
    everything it has learned since" rather than "what would Sentinel have
    decided at the time" — the latter would need a second historical store
    snapshot this codebase does not keep. `memory_bias` follows the same
    rule for consistency, and combining the two the same way the live
    orchestrator does (`learning.merge_bias`) is what keeps replay an
    honest preview of live behaviour rather than a second, drifting
    implementation of it.
    `recovery_validation_available`/`chaos_surface_available` are plain
    booleans the caller computes once for this incident's fixed target
    (`RecoveryValidator.is_available_for(target)`, and the chaos-surface
    check `_policy_context` already does) — passed as facts, not as client
    objects, so this module has no way to call anything live even if it
    wanted to.
    """
    replayed_at = time.time()

    if incident.evidence is None:
        return _empty_result(
            incident,
            from_scratch=from_scratch,
            replayed_at=replayed_at,
            note="no evidence was recorded for this incident; nothing to replay",
        )

    evidence = incident.evidence
    reference_time = incident.created_at
    notes: list[str] = []

    findings = correlation.correlate(
        incident=incident,
        evidence=evidence,
        correlation_window_minutes=policy.config.deployment_correlation_window_minutes,
        cpu_threshold_cores=cpu_threshold_cores,
        error_rate_threshold=error_rate_threshold,
        p95_threshold_seconds=p95_threshold_seconds,
        now=reference_time,
    )

    hypothesis = rca.analyse(incident, evidence, findings)

    recorded = incident.hypothesis
    recorded_root_cause = recorded.root_cause.value if recorded else None
    root_cause_changed = recorded is not None and recorded.root_cause != hypothesis.root_cause
    if root_cause_changed:
        notes.append(
            f"rules-only replay concludes {hypothesis.root_cause.value}, which differs "
            f"from the recorded diagnosis ({recorded_root_cause}). Replay always uses "
            "the deterministic rules pass (never the LLM), so this can mean the "
            "recorded hypothesis was LLM-adjusted, or that the RCA rules changed "
            "since this incident."
        )

    learning_bias = learning.load_bias(hypothesis.root_cause, store)

    memory_bias: dict[str, float] = {}
    if incident.app:
        try:
            current_signature = compute_signature(evidence)
            past_records = store.list_terminal_incidents_for_app(
                incident.app, exclude_incident_id=incident.id
            )
            similar = memory.find_similar_incidents(current_signature, past_records)
            memory_bias = memory.build_similarity_bias(similar)
        except Exception as exc:  # noqa: BLE001 - memory is never load-bearing, even in replay
            notes.append(f"operational memory lookup failed during replay: {str(exc)[:200]}")
            memory_bias = {}
    combined_bias = learning.merge_bias(learning_bias, memory_bias)

    decision_incident = incident
    if from_scratch and incident.attempts:
        decision_incident = dataclasses.replace(incident, attempts=[])
        notes.append(
            "from_scratch=True: this incident's own attempt history was ignored, so "
            "actions already tried are reconsidered as candidates too"
        )

    plans = DecisionEngine(min_replicas=min_replicas, max_replicas=max_replicas).candidates(
        decision_incident, hypothesis, findings, learning_bias=combined_bias
    )

    if not plans:
        return ReplayResult(
            incident_id=incident.id,
            reference_time=reference_time,
            replayed_at=replayed_at,
            from_scratch=from_scratch,
            hypothesis=hypothesis.to_dict(),
            recorded_root_cause=recorded_root_cause,
            hypothesis_root_cause_changed=root_cause_changed,
            learning_bias=learning_bias,
            memory_bias=memory_bias,
            candidates=[],
            chosen_action=None,
            would_escalate=True,
            notes=notes,
        )

    rollback_reversible = findings.revision_count >= 2
    cooldown = float(policy.config.action_cooldown_seconds)
    other_actions = (
        store.recent_executed_actions(
            incident.app, since=reference_time - cooldown, exclude_incident_id=incident.id
        )
        if incident.app
        else {}
    )
    current_replicas = evidence.deployment.get("desired_replicas") if evidence.deployment else None
    target_is_stateful = bool(
        incident.target_deployment and "postgres" in incident.target_deployment.lower()
    )

    candidate_replays: list[CandidateReplay] = []
    chosen_action: str | None = None
    for index, plan in enumerate(plans):
        risk_assessment = risk.assess_risk(
            incident,
            plan,
            candidate_index=index,
            rollback_reversible=rollback_reversible,
            recovery_validation_available=recovery_validation_available,
        )
        context = PolicyContext(
            other_incident_actions=other_actions,
            previous_revision_exists=findings.previous_revision is not None,
            deployment_history_count=findings.revision_count,
            last_deploy_age_seconds=findings.recent_deployment_age_seconds,
            deploy_correlates_with_onset=findings.deploy_correlates_with_onset,
            rollback_reversible=rollback_reversible,
            recovery_validation_available=recovery_validation_available,
            current_replicas=current_replicas,
            chaos_surface_available=chaos_surface_available,
            target_is_stateful=target_is_stateful,
            risk=risk_assessment.to_dict(),
        )
        verdict = policy.evaluate(incident, plan, context, now=reference_time)
        would_execute = verdict.allowed and chosen_action is None
        if would_execute:
            chosen_action = plan.action.value
        candidate_replays.append(
            CandidateReplay(
                candidate_index=index,
                action=plan.action.value,
                namespace=plan.params.namespace,
                deployment=plan.params.deployment,
                confidence=plan.confidence,
                rationale=plan.rationale,
                risk=risk_assessment.to_dict(),
                allowed=verdict.allowed,
                denial_reason=verdict.reason.value if verdict.reason else None,
                denial_detail=verdict.detail,
                would_execute=would_execute,
            )
        )

    return ReplayResult(
        incident_id=incident.id,
        reference_time=reference_time,
        replayed_at=replayed_at,
        from_scratch=from_scratch,
        hypothesis=hypothesis.to_dict(),
        recorded_root_cause=recorded_root_cause,
        hypothesis_root_cause_changed=root_cause_changed,
        learning_bias=learning_bias,
        memory_bias=memory_bias,
        candidates=candidate_replays,
        chosen_action=chosen_action,
        would_escalate=chosen_action is None,
        notes=notes,
    )
