"""
SENTINEL AGENT EVALUATION — ground-truth scoring using chaos scenarios.

Measures autonomous decision quality against a KNOWN right answer. The only
source of a known right answer in this system is a deliberately-injected
chaos scenario (routers/chaos_scenarios.py's `SCENARIOS` table, each now
carrying `expected_root_cause`/`expected_action`/`expected_app` where the RCA
rules produce exactly one unambiguous outcome — see that table's own
comment for why two scenarios deliberately carry none). Metrics that do NOT
need a known right answer (first-action success rate, escalation rate,
policy rejection rate, investigation latency, time to recovery, and so on)
are computed straight from the general incident population instead — see
routers/performance.py, extended alongside this module — because every
incident already carries what those need, chaos-triggered or not.

### Why "record, then resolve later" instead of "trigger and wait"

Triggering a scenario is a live AWS SSM operation with no offline/test
equivalent (routers/chaos_scenarios.py), and the incident it produces can
take anywhere from seconds to the alert's `for:` duration to appear. Coupling
"start the fault" and "wait for the incident" into one request would mean
either blocking an HTTP call for an unbounded time or standing up a
background poller — more machinery than a demo/validation feature justifies.

Instead, `create_run` only records the expectation and a trigger timestamp —
it never touches AWS, so it needs no cloud credentials and can be unit
tested completely offline. `resolve_run` is called lazily, at *read* time
(the same choice lifecycle/causal_graph.py and lifecycle/memory.py already
made: derive from the store, do not invent a second live process), and looks
for the first incident on the run's app created at/after the trigger time.
Once one is found AND has reached a terminal status, the run is resolved
once and its comparison is persisted so later reads do not recompute it.

### What is and is not scored

`root_cause_correct` and `decision_matched_expected` are computed only when
the scenario has a known `expected_root_cause`/`expected_action` — both stay
`None` (not `False`) when there is nothing to compare against, so an
"unknown outcome" scenario can never silently count as a wrong answer.
`decision_matched_expected` compares against the FIRST executed action only
(what Sentinel actually tried first), because that is the one Sentinel's own
decision, distinct from whatever a later fallback in the same incident did.
"""
from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any

from app.models.incident import Incident, IncidentStatus

TERMINAL_STATUSES = frozenset(
    {
        IncidentStatus.RESOLVED.value,
        IncidentStatus.ESCALATED.value,
        IncidentStatus.AUTO_RESOLVED.value,
    }
)

# How long an evaluation run waits for a matching incident before giving up
# honestly (marking itself resolved with no incident found) rather than
# staying "pending" forever. Generous: alert `for:` durations plus
# investigation time, with headroom.
LINK_WINDOW_SECONDS = 30 * 60

_FIELDS = (
    "id",
    "scenario",
    "app",
    "expected_root_cause",
    "expected_action",
    "triggered_at",
    "resolved",
    "incident_id",
    "resolved_at",
    "actual_root_cause",
    "actual_first_action",
    "root_cause_correct",
    "decision_matched_expected",
    "final_status",
    "escalated",
    "remediation_succeeded",
    "recovery_validated",
    "notes",
)


@dataclass
class EvaluationRun:
    id: str
    scenario: str
    app: str
    expected_root_cause: str | None
    expected_action: str | None
    triggered_at: float
    resolved: bool = False
    incident_id: str | None = None
    resolved_at: float | None = None
    actual_root_cause: str | None = None
    actual_first_action: str | None = None
    # None means "not evaluable" (no ground truth to compare against);
    # True/False mean a comparison was actually made.
    root_cause_correct: bool | None = None
    decision_matched_expected: bool | None = None
    final_status: str | None = None
    escalated: bool | None = None
    remediation_succeeded: bool | None = None
    recovery_validated: bool | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {f: getattr(self, f) for f in _FIELDS}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvaluationRun":
        return cls(**{f: data.get(f) for f in _FIELDS if f != "resolved"}, resolved=bool(data.get("resolved")))


def create_run(
    run_id: str,
    scenario: str,
    app: str,
    *,
    expected_root_cause: str | None,
    expected_action: str | None,
    triggered_at: float | None = None,
) -> EvaluationRun:
    """Record an evaluation's expectation. Does not trigger anything."""
    return EvaluationRun(
        id=run_id,
        scenario=scenario,
        app=app,
        expected_root_cause=expected_root_cause,
        expected_action=expected_action,
        triggered_at=triggered_at if triggered_at is not None else time.time(),
    )


def resolve_run(run: EvaluationRun, store: Any, *, now: float | None = None) -> EvaluationRun:
    """Try to link `run` to the incident it caused and score it.

    Never mutates `run` — returns it unchanged (still pending) when nothing
    new is known yet, or a new `EvaluationRun` reflecting what was found.
    Callers persist the result themselves (see routers/evaluation.py) so
    this stays a pure function of its inputs, like every other lifecycle
    stage in this codebase.
    """
    if run.resolved:
        return run
    now = now if now is not None else time.time()

    record = store.find_incident_after(run.app, run.triggered_at)
    if record is None:
        if now - run.triggered_at > LINK_WINDOW_SECONDS:
            return dataclasses.replace(
                run,
                resolved=True,
                notes=(
                    f"no incident appeared on '{run.app}' within "
                    f"{LINK_WINDOW_SECONDS}s of triggering this scenario"
                ),
            )
        return run  # still waiting - not an error, just not there yet

    incident = Incident.from_dict(record)
    if incident.status.value not in TERMINAL_STATUSES:
        # Found the incident, but it is still in flight - link it (so a
        # future call does not have to search again) without scoring yet.
        return dataclasses.replace(run, incident_id=incident.id)

    hypothesis = incident.hypothesis
    actual_root_cause = hypothesis.root_cause.value if hypothesis else None
    root_cause_correct = (
        actual_root_cause == run.expected_root_cause
        if run.expected_root_cause is not None
        else None
    )

    executed = [a for a in incident.attempts if a.result is not None]
    first = executed[0] if executed else None
    actual_first_action = first.plan.action.value if first else None
    decision_matched_expected = (
        actual_first_action == run.expected_action
        if run.expected_action is not None
        else None
    )
    remediation_succeeded = first.result.succeeded if first and first.result else None
    recovery_validated = bool(first.validation and first.validation.passed) if first else None

    return dataclasses.replace(
        run,
        resolved=True,
        incident_id=incident.id,
        resolved_at=now,
        actual_root_cause=actual_root_cause,
        actual_first_action=actual_first_action,
        root_cause_correct=root_cause_correct,
        decision_matched_expected=decision_matched_expected,
        final_status=incident.status.value,
        escalated=incident.escalated,
        remediation_succeeded=remediation_succeeded,
        recovery_validated=recovery_validated,
    )


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def aggregate_evaluation_metrics(runs: list[EvaluationRun]) -> dict[str, Any]:
    """Ground-truth metrics across resolved runs that actually found an
    incident (a "no incident appeared" run contributes to nothing but its
    own sample count — it is not evidence Sentinel decided anything)."""
    concluded = [r for r in runs if r.resolved and r.incident_id is not None]
    scored_root_cause = [r for r in concluded if r.root_cause_correct is not None]
    scored_action = [r for r in concluded if r.decision_matched_expected is not None]

    return {
        "sample_size": {
            "total_runs": len(runs),
            "pending_runs": sum(1 for r in runs if not r.resolved),
            "concluded_runs": len(concluded),
            "root_cause_scored": len(scored_root_cause),
            "decision_scored": len(scored_action),
        },
        "rca_correctness_rate": _rate(
            sum(1 for r in scored_root_cause if r.root_cause_correct), len(scored_root_cause)
        ),
        "decision_accuracy_rate": _rate(
            sum(1 for r in scored_action if r.decision_matched_expected), len(scored_action)
        ),
    }
