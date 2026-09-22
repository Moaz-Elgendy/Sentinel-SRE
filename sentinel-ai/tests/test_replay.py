"""
INCIDENT REPLAY / WHAT-IF SIMULATION tests (lifecycle/replay.py).

Builds real `Evidence` bundles and lets `replay_incident` run the actual
correlation/RCA/decision/risk/policy pipeline over them, rather than
hand-crafting `CorrelationFindings`/`Hypothesis` — that pipeline is exactly
what this module wraps, so a test that bypassed it would not be testing
replay at all. Each scenario is chosen to be unambiguous under the rules in
correlation.py/rca.py/policy.py (see their own test files for exhaustive
coverage of those rules individually).
"""
from __future__ import annotations

import time

from app.lifecycle.policy import PolicyConfig, PolicyEngine
from app.lifecycle.replay import replay_incident
from app.models.incident import (
    AttemptRecord,
    Evidence,
    Incident,
    RemediationAction,
    Severity,
)

from .conftest import executed_attempt, make_plan


class _FakeStore:
    """Minimal store: only the two read-only methods replay touches."""

    def __init__(self, other_actions=None, action_stats=None):
        self._other_actions = other_actions or {}
        self._action_stats = action_stats or {}

    def recent_executed_actions(self, app, since, exclude_incident_id=None):
        return dict(self._other_actions)

    def action_stats(self, root_cause):
        return self._action_stats.get(root_cause, {})


def _replay(incident, *, store=None, from_scratch=False, **overrides):
    kwargs = dict(
        policy=PolicyEngine(PolicyConfig(
            allowed_namespaces=frozenset({"citizen-portal"}),
            allowed_deployments=frozenset({"citizen-service"}),
            denied_deployments=frozenset(),
            denied_namespaces=frozenset(),
        )),
        store=store or _FakeStore(),
        min_replicas=1,
        max_replicas=3,
        cpu_threshold_cores=0.8,
        error_rate_threshold=0.1,
        p95_threshold_seconds=1.0,
        recovery_validation_available=True,
        chaos_surface_available=False,
        from_scratch=from_scratch,
    )
    kwargs.update(overrides)
    return replay_incident(incident, **kwargs)


def _bad_deployment_incident(now: float) -> Incident:
    return Incident(
        id="INC-REPLAY-BAD-DEPLOY",
        fingerprint="fp-1",
        alertname="HighHTTPErrorRate",
        severity=Severity.CRITICAL,
        app="citizen-service",
        namespace="citizen-portal",
        created_at=now,
        evidence=Evidence(
            error_rate=0.5,
            deployment={"desired_replicas": 2, "available_replicas": 2},
            replicaset_history=[
                {
                    "revision": 5,
                    "created_at": now - 120,
                    "images": ["repo/citizen-service:v2"],
                    "replicas": 2,
                    "ready_replicas": 2,
                    "images_valid": True,
                },
                {
                    "revision": 4,
                    "created_at": now - 3600,
                    "images": ["repo/citizen-service:v1"],
                    "images_valid": True,
                },
            ],
        ),
    )


def test_no_recorded_evidence_returns_an_honest_empty_result():
    incident = Incident(
        id="INC-NO-EVIDENCE",
        fingerprint="fp-2",
        alertname="HighHTTPErrorRate",
        severity=Severity.CRITICAL,
        app="citizen-service",
        namespace="citizen-portal",
    )
    result = _replay(incident)
    assert result.candidates == []
    assert result.would_escalate is True
    assert result.hypothesis is None
    assert "no evidence" in result.notes[0]


def test_bad_deployment_replay_recommends_rollback_and_allows_it():
    now = time.time()
    incident = _bad_deployment_incident(now)
    result = _replay(incident)

    assert result.hypothesis["root_cause"] == "bad_deployment"
    assert [c.action for c in result.candidates] == [
        "rollback_deployment",
        "restart_deployment",
    ]
    rollback = result.candidates[0]
    assert rollback.allowed is True
    assert rollback.would_execute is True
    assert rollback.risk["blast_radius_scope"] == "single_workload"
    assert result.chosen_action == "rollback_deployment"
    assert result.would_escalate is False
    # Only the first allowed candidate is ever marked as the one that would
    # actually run, however many other candidates are also allowed.
    assert result.candidates[1].would_execute is False


def test_reference_time_is_the_incidents_created_at_not_wall_clock():
    """A deploy that happened long before `created_at` must NOT look recent
    just because replay runs long after the incident itself did — this is
    exactly the "old incident replayed today" bug the module docstring
    warns about."""
    long_ago = time.time() - (365 * 24 * 3600)
    incident = _bad_deployment_incident(long_ago)
    result = _replay(incident)
    assert result.hypothesis["root_cause"] == "bad_deployment"
    assert result.candidates[0].action == "rollback_deployment"


def test_denylisted_target_is_denied_even_though_rules_recommend_it():
    now = time.time()
    incident = _bad_deployment_incident(now)
    store = _FakeStore()
    result = _replay(
        incident,
        store=store,
        policy=PolicyEngine(PolicyConfig(
            allowed_namespaces=frozenset({"citizen-portal"}),
            allowed_deployments=frozenset({"citizen-service"}),
            denied_deployments=frozenset({"citizen-service"}),
            denied_namespaces=frozenset(),
        )),
    )
    assert result.candidates[0].allowed is False
    assert result.candidates[0].would_execute is False
    assert result.chosen_action is None
    assert result.would_escalate is True


def test_from_scratch_reconsiders_an_already_attempted_action():
    now = time.time()
    incident = _bad_deployment_incident(now)
    incident.attempts = [executed_attempt(make_plan(RemediationAction.ROLLBACK_DEPLOYMENT))]

    default_result = _replay(incident)
    assert [c.action for c in default_result.candidates] == ["restart_deployment"]

    scratch_result = _replay(incident, from_scratch=True)
    assert [c.action for c in scratch_result.candidates] == [
        "rollback_deployment",
        "restart_deployment",
    ]
    assert any("from_scratch=True" in n for n in scratch_result.notes)
    # The original incident's own attempts must be untouched by replay.
    assert len(incident.attempts) == 1


def test_root_cause_drift_from_the_recorded_hypothesis_is_flagged():
    now = time.time()
    incident = _bad_deployment_incident(now)
    from app.models.incident import Hypothesis, RootCause

    incident.hypothesis = Hypothesis(
        root_cause=RootCause.MEMORY_LEAK,
        confidence=0.9,
        reasoning="recorded (possibly LLM-adjusted) diagnosis",
        recommended_action=RemediationAction.RESTART_DEPLOYMENT,
    )
    result = _replay(incident)
    assert result.recorded_root_cause == "memory_leak"
    assert result.hypothesis["root_cause"] == "bad_deployment"
    assert result.hypothesis_root_cause_changed is True
    assert any("differs from the recorded diagnosis" in n for n in result.notes)


def test_unknown_root_cause_has_no_candidates_and_would_escalate():
    now = time.time()
    incident = Incident(
        id="INC-UNKNOWN",
        fingerprint="fp-3",
        alertname="HighHTTPErrorRate",
        severity=Severity.CRITICAL,
        app="citizen-service",
        namespace="citizen-portal",
        created_at=now,
        evidence=Evidence(),  # nothing recognisable
    )
    result = _replay(incident)
    assert result.candidates == []
    assert result.chosen_action is None
    assert result.would_escalate is True


def test_replay_never_mutates_the_incident():
    now = time.time()
    incident = _bad_deployment_incident(now)
    before_attempts = list(incident.attempts)
    before_timeline = list(incident.timeline)
    before_status = incident.status
    _replay(incident)
    assert incident.attempts == before_attempts
    assert incident.timeline == before_timeline
    assert incident.status == before_status
