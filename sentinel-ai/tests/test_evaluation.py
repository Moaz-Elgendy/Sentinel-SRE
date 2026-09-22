"""
SENTINEL AGENT EVALUATION tests (lifecycle/evaluation.py).

Builds real `Incident`/`AttemptRecord`/`Hypothesis` objects and round-trips
them through `.to_dict()`, exactly the shape `store.find_incident_after`
returns in production, rather than hand-writing dicts that could drift from
the real schema.
"""
from __future__ import annotations

import time

from app.lifecycle.evaluation import (
    LINK_WINDOW_SECONDS,
    EvaluationRun,
    aggregate_evaluation_metrics,
    create_run,
    resolve_run,
)
from app.models.incident import (
    ActionParams,
    ActionPlan,
    AttemptRecord,
    Evidence,
    Hypothesis,
    Incident,
    IncidentStatus,
    RemediationAction,
    RemediationResult,
    RootCause,
    Severity,
    ValidationOutcome,
    ValidationReport,
)


class _FakeStore:
    def __init__(self, incident_dict=None):
        self._incident_dict = incident_dict

    def find_incident_after(self, app, since):
        return self._incident_dict


class _ExplodingStore:
    """A store that fails any call - proves `resolve_run` never touches the
    store for an already-resolved run."""

    def find_incident_after(self, app, since):
        raise AssertionError("resolve_run must not query the store for a resolved run")


def _make_incident(
    *,
    root_cause=RootCause.MEMORY_LEAK,
    action=RemediationAction.RESTART_DEPLOYMENT,
    succeeded=True,
    validated=True,
    status=IncidentStatus.RESOLVED,
    escalated=False,
) -> dict:
    plan = ActionPlan(
        action=action,
        params=ActionParams(namespace="citizen-portal", deployment="citizen-service"),
        confidence=0.95,
        rationale="test",
    )
    attempt = AttemptRecord(
        plan=plan,
        result=RemediationResult(
            action=action, params=plan.params, succeeded=succeeded, started_at=time.time()
        ),
        validation=ValidationReport(
            outcome=ValidationOutcome.PASSED if validated else ValidationOutcome.FAILED
        ),
    )
    incident = Incident(
        id="INC-EVAL-0001",
        fingerprint="fp-eval-1",
        alertname="MemoryLeakSuspected",
        severity=Severity.WARNING,
        app="citizen-service",
        namespace="citizen-portal",
        status=status,
        escalated=escalated,
        evidence=Evidence(),
        hypothesis=Hypothesis(
            root_cause=root_cause,
            confidence=0.92,
            reasoning="test",
            recommended_action=action,
        ),
        attempts=[attempt],
    )
    return incident.to_dict()


def test_create_run_never_touches_a_store():
    run = create_run(
        "eval-1",
        "memory-leak",
        "citizen-service",
        expected_root_cause="memory_leak",
        expected_action="restart_deployment",
    )
    assert run.resolved is False
    assert run.incident_id is None


def test_an_already_resolved_run_is_returned_unchanged_without_touching_the_store():
    run = EvaluationRun(
        id="eval-1",
        scenario="memory-leak",
        app="citizen-service",
        expected_root_cause="memory_leak",
        expected_action="restart_deployment",
        triggered_at=time.time(),
        resolved=True,
    )
    result = resolve_run(run, _ExplodingStore())
    assert result is run


def test_no_incident_yet_within_the_window_stays_pending():
    run = create_run(
        "eval-1", "memory-leak", "citizen-service",
        expected_root_cause="memory_leak", expected_action="restart_deployment",
        triggered_at=time.time(),
    )
    result = resolve_run(run, _FakeStore(incident_dict=None))
    assert result.resolved is False
    assert result.incident_id is None


def test_no_incident_after_the_window_closes_gives_up_honestly():
    run = create_run(
        "eval-1", "memory-leak", "citizen-service",
        expected_root_cause="memory_leak", expected_action="restart_deployment",
        triggered_at=time.time() - LINK_WINDOW_SECONDS - 1,
    )
    result = resolve_run(run, _FakeStore(incident_dict=None))
    assert result.resolved is True
    assert result.incident_id is None
    assert "no incident appeared" in result.notes


def test_incident_found_but_not_yet_terminal_is_linked_not_scored():
    run = create_run(
        "eval-1", "memory-leak", "citizen-service",
        expected_root_cause="memory_leak", expected_action="restart_deployment",
    )
    store = _FakeStore(incident_dict=_make_incident(status=IncidentStatus.REMEDIATING))
    result = resolve_run(run, store)
    assert result.resolved is False
    assert result.incident_id == "INC-EVAL-0001"
    assert result.root_cause_correct is None


def test_terminal_incident_matching_expectations_scores_correct_on_both_axes():
    run = create_run(
        "eval-1", "memory-leak", "citizen-service",
        expected_root_cause="memory_leak", expected_action="restart_deployment",
    )
    store = _FakeStore(incident_dict=_make_incident(
        root_cause=RootCause.MEMORY_LEAK,
        action=RemediationAction.RESTART_DEPLOYMENT,
        succeeded=True,
        validated=True,
    ))
    result = resolve_run(run, store)
    assert result.resolved is True
    assert result.root_cause_correct is True
    assert result.decision_matched_expected is True
    assert result.remediation_succeeded is True
    assert result.recovery_validated is True
    assert result.final_status == "resolved"


def test_terminal_incident_with_a_different_root_cause_scores_incorrect():
    run = create_run(
        "eval-1", "memory-leak", "citizen-service",
        expected_root_cause="memory_leak", expected_action="restart_deployment",
    )
    store = _FakeStore(incident_dict=_make_incident(root_cause=RootCause.CPU_SATURATION))
    result = resolve_run(run, store)
    assert result.root_cause_correct is False
    assert result.actual_root_cause == "cpu_saturation"


def test_a_scenario_with_no_ground_truth_is_never_scored_even_when_resolved():
    """high-cpu/crashloop carry no expected_root_cause/expected_action —
    resolving must never turn "we don't know" into a False."""
    run = create_run(
        "eval-1", "high-cpu", "citizen-service",
        expected_root_cause=None, expected_action=None,
    )
    store = _FakeStore(incident_dict=_make_incident(root_cause=RootCause.CPU_SATURATION))
    result = resolve_run(run, store)
    assert result.resolved is True
    assert result.root_cause_correct is None
    assert result.decision_matched_expected is None


def test_escalated_incident_has_no_executed_action_but_still_scores_root_cause():
    run = create_run(
        "eval-1", "bad-deployment", "citizen-service",
        expected_root_cause="bad_deployment", expected_action=None,
    )
    incident = Incident(
        id="INC-EVAL-ESC",
        fingerprint="fp-eval-2",
        alertname="HighHTTPErrorRate",
        severity=Severity.CRITICAL,
        app="citizen-service",
        namespace="citizen-portal",
        status=IncidentStatus.ESCALATED,
        escalated=True,
        evidence=Evidence(),
        hypothesis=Hypothesis(
            root_cause=RootCause.BAD_DEPLOYMENT,
            confidence=0.90,
            reasoning="test",
            recommended_action=RemediationAction.ROLLBACK_DEPLOYMENT,
        ),
        attempts=[],
    )
    store = _FakeStore(incident_dict=incident.to_dict())
    result = resolve_run(run, store)
    assert result.root_cause_correct is True
    assert result.decision_matched_expected is None  # no expected_action to compare
    assert result.actual_first_action is None
    assert result.escalated is True


def test_aggregate_metrics_only_counts_concluded_runs_with_an_incident():
    runs = [
        # Concluded, ground truth, correct.
        resolve_run(
            create_run("e1", "memory-leak", "citizen-service",
                       expected_root_cause="memory_leak", expected_action="restart_deployment"),
            _FakeStore(incident_dict=_make_incident()),
        ),
        # Concluded, ground truth, wrong root cause.
        resolve_run(
            create_run("e2", "memory-leak", "citizen-service",
                       expected_root_cause="memory_leak", expected_action="restart_deployment"),
            _FakeStore(incident_dict=_make_incident(root_cause=RootCause.SERVICE_DOWN)),
        ),
        # Concluded, no ground truth at all.
        resolve_run(
            create_run("e3", "high-cpu", "citizen-service",
                       expected_root_cause=None, expected_action=None),
            _FakeStore(incident_dict=_make_incident()),
        ),
        # Gave up - no incident ever appeared.
        resolve_run(
            create_run("e4", "memory-leak", "citizen-service",
                       expected_root_cause="memory_leak", expected_action="restart_deployment",
                       triggered_at=time.time() - LINK_WINDOW_SECONDS - 1),
            _FakeStore(incident_dict=None),
        ),
        # Still pending.
        create_run("e5", "memory-leak", "citizen-service",
                   expected_root_cause="memory_leak", expected_action="restart_deployment"),
    ]
    metrics = aggregate_evaluation_metrics(runs)
    assert metrics["sample_size"] == {
        "total_runs": 5,
        "pending_runs": 1,
        "concluded_runs": 3,  # e1, e2, e3 — e4 gave up (no incident_id), e5 still pending
        "root_cause_scored": 2,  # e1, e2 (e3 has no ground truth)
        "decision_scored": 2,
    }
    assert metrics["rca_correctness_rate"] == 0.5  # 1 of 2 (e2's root cause was wrong)
    # e2's *action* still matches the expectation even though its root cause
    # didn't - the two axes are scored independently, on purpose.
    assert metrics["decision_accuracy_rate"] == 1.0


def test_aggregate_metrics_on_no_runs_is_honestly_empty_not_zero():
    metrics = aggregate_evaluation_metrics([])
    assert metrics["rca_correctness_rate"] is None
    assert metrics["decision_accuracy_rate"] is None


def test_run_to_dict_and_from_dict_round_trip():
    run = resolve_run(
        create_run("e1", "memory-leak", "citizen-service",
                   expected_root_cause="memory_leak", expected_action="restart_deployment"),
        _FakeStore(incident_dict=_make_incident()),
    )
    restored = EvaluationRun.from_dict(run.to_dict())
    assert restored == run
