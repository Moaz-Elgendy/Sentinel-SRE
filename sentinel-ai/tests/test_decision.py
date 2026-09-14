"""
Decision Engine tests.

What matters here is the *ordering and content* of the candidate list, because
that is what "pick the next safe action" means in practice, and because a
badly-ordered ladder can turn a small incident into a large one (falling back
to a rollback for a reason unrelated to code, for example).

All pure — no cluster, no network.
"""
from __future__ import annotations

import pytest

from app.lifecycle.correlation import CorrelationFindings
from app.lifecycle.decision import ACTION_LADDER, FALLBACK_DISCOUNT, DecisionEngine
from app.models.incident import (
    Hypothesis,
    RemediationAction,
    RootCause,
)

from .conftest import executed_attempt, make_plan


@pytest.fixture
def engine():
    return DecisionEngine(min_replicas=1, max_replicas=3)


def hypothesis(
    root_cause: RootCause,
    action: RemediationAction,
    confidence: float = 0.96,
) -> Hypothesis:
    return Hypothesis(
        root_cause=root_cause,
        confidence=confidence,
        reasoning="test",
        recommended_action=action,
    )


def findings(**kwargs) -> CorrelationFindings:
    f = CorrelationFindings()
    for key, value in kwargs.items():
        setattr(f, key, value)
    return f


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------
def test_chaos_fault_prefers_reset_then_restart(engine, incident):
    plans = engine.candidates(
        incident,
        hypothesis(RootCause.CHAOS_DATABASE_FAULT, RemediationAction.RESET_CHAOS_FAULT),
        findings(),
    )
    assert [p.action for p in plans] == [
        RemediationAction.RESET_CHAOS_FAULT,
        RemediationAction.RESTART_DEPLOYMENT,
    ]


def test_bad_deployment_prefers_rollback(engine, incident):
    plans = engine.candidates(
        incident,
        hypothesis(RootCause.BAD_DEPLOYMENT, RemediationAction.ROLLBACK_DEPLOYMENT),
        findings(previous_revision=2, revision_count=3),
    )
    assert plans[0].action is RemediationAction.ROLLBACK_DEPLOYMENT
    assert plans[0].params.target_revision == 2


def test_rca_recommendation_is_placed_first(engine, incident):
    """Even when the static ladder would order differently.

    RCA saw the evidence; the ladder is a table. CPU_SATURATION's ladder is
    (scale, restart), so recommending restart must move it to the front.
    """
    plans = engine.candidates(
        incident,
        hypothesis(RootCause.CPU_SATURATION, RemediationAction.RESTART_DEPLOYMENT),
        findings(),
    )
    assert plans[0].action is RemediationAction.RESTART_DEPLOYMENT


def test_rollback_is_never_a_fallback_for_any_root_cause():
    """A rollback must only ever be a *primary* candidate.

    Falling back to a rollback means changing what code is running for a
    reason unrelated to the code. This asserts the property across the whole
    table rather than one case, so a future edit to ACTION_LADDER cannot
    quietly introduce it.
    """
    for root_cause, ladder in ACTION_LADDER.items():
        for index, action in enumerate(ladder):
            if action is RemediationAction.ROLLBACK_DEPLOYMENT:
                assert index == 0, (
                    f"{root_cause.value} has rollback at fallback position {index}"
                )


def test_scale_is_never_a_fallback_for_a_fault_shaped_cause():
    """Adding replicas to a broken service multiplies the breakage."""
    fault_causes = {
        RootCause.CHAOS_DATABASE_FAULT,
        RootCause.CHAOS_HTTP_FAULT,
        RootCause.CHAOS_LATENCY_FAULT,
        RootCause.CHAOS_NOTIFICATION_FAULT,
        RootCause.BAD_DEPLOYMENT,
        RootCause.POD_CRASH_LOOP,
        RootCause.MEMORY_LEAK,
        RootCause.SERVICE_DOWN,
    }
    for root_cause in fault_causes:
        ladder = ACTION_LADDER.get(root_cause, ())
        assert RemediationAction.SCALE_DEPLOYMENT not in ladder, root_cause.value


# ---------------------------------------------------------------------------
# Confidence handling
# ---------------------------------------------------------------------------
def test_fallbacks_are_discounted(engine, incident):
    plans = engine.candidates(
        incident,
        hypothesis(
            RootCause.CHAOS_HTTP_FAULT, RemediationAction.RESET_CHAOS_FAULT, confidence=0.96
        ),
        findings(),
    )
    assert plans[0].confidence == pytest.approx(0.96)
    assert plans[1].confidence == pytest.approx(0.96 - FALLBACK_DISCOUNT)


def test_learning_bias_can_only_lower_confidence(incident):
    """The bias multiplier is <= 1.0 by construction in learning.py.

    Here we confirm the Decision Engine applies it multiplicatively, so a
    penalty reduces confidence and can push a candidate below a policy
    threshold — the only direction learning is allowed to move things.
    """
    engine = DecisionEngine(
        min_replicas=1, max_replicas=3, learning_bias={"restart_deployment": 0.85}
    )
    plans = engine.candidates(
        incident,
        hypothesis(RootCause.MEMORY_LEAK, RemediationAction.RESTART_DEPLOYMENT, 0.95),
        findings(),
    )
    assert plans[0].confidence == pytest.approx(0.95 * 0.85)
    assert plans[0].confidence < 0.90  # now below the restart threshold


# ---------------------------------------------------------------------------
# Impossible candidates are omitted, not emitted-and-denied
# ---------------------------------------------------------------------------
def test_no_rollback_candidate_without_a_previous_revision(engine, incident):
    plans = engine.candidates(
        incident,
        hypothesis(RootCause.BAD_DEPLOYMENT, RemediationAction.ROLLBACK_DEPLOYMENT),
        findings(previous_revision=None),
    )
    assert RemediationAction.ROLLBACK_DEPLOYMENT not in [p.action for p in plans]


def test_no_scale_candidate_when_already_at_max(engine, incident):
    incident.evidence.deployment = {"desired_replicas": 3}
    plans = engine.candidates(
        incident,
        hypothesis(RootCause.CAPACITY_SHORTFALL, RemediationAction.SCALE_DEPLOYMENT),
        findings(),
    )
    assert plans == []


def test_scale_target_is_current_plus_one(engine, incident):
    incident.evidence.deployment = {"desired_replicas": 1}
    plans = engine.candidates(
        incident,
        hypothesis(RootCause.CAPACITY_SHORTFALL, RemediationAction.SCALE_DEPLOYMENT),
        findings(),
    )
    assert plans[0].params.replicas == 2


def test_no_candidates_without_a_target_deployment(engine, incident):
    incident.app = None
    plans = engine.candidates(
        incident,
        hypothesis(RootCause.MEMORY_LEAK, RemediationAction.RESTART_DEPLOYMENT),
        findings(),
    )
    assert plans == []


def test_no_candidates_when_rca_recommends_escalation(engine, incident):
    plans = engine.candidates(
        incident,
        hypothesis(RootCause.DOWNSTREAM_DEPENDENCY, RemediationAction.ESCALATE, 0.7),
        findings(),
    )
    assert plans == []


def test_unknown_root_cause_yields_no_candidates(engine, incident):
    """No confident cause means no autonomous action. Escalate instead."""
    plans = engine.candidates(
        incident,
        hypothesis(RootCause.UNKNOWN, RemediationAction.RESTART_DEPLOYMENT, 0.99),
        findings(),
    )
    assert plans == []


def test_database_failure_yields_no_candidates(engine, incident):
    plans = engine.candidates(
        incident,
        hypothesis(RootCause.DATABASE_FAILURE, RemediationAction.RESTART_DEPLOYMENT, 0.99),
        findings(),
    )
    assert plans == []


# ---------------------------------------------------------------------------
# Already-attempted filtering (the anti-loop property)
# ---------------------------------------------------------------------------
def test_executed_actions_are_not_offered_again(engine, incident):
    incident.attempts.append(
        executed_attempt(make_plan(RemediationAction.RESET_CHAOS_FAULT))
    )
    plans = engine.candidates(
        incident,
        hypothesis(RootCause.CHAOS_HTTP_FAULT, RemediationAction.RESET_CHAOS_FAULT),
        findings(),
    )
    assert [p.action for p in plans] == [RemediationAction.RESTART_DEPLOYMENT]


def test_denied_actions_are_still_offered(engine, incident):
    """A policy denial means it was never executed, so it may be reconsidered
    once the evidence changes (e.g. a deploy correlation appears)."""
    from app.models.incident import AttemptRecord

    incident.attempts.append(
        AttemptRecord(plan=make_plan(RemediationAction.RESET_CHAOS_FAULT), result=None)
    )
    plans = engine.candidates(
        incident,
        hypothesis(RootCause.CHAOS_HTTP_FAULT, RemediationAction.RESET_CHAOS_FAULT),
        findings(),
    )
    assert plans[0].action is RemediationAction.RESET_CHAOS_FAULT


def test_ladder_exhaustion_gives_an_empty_list(engine, incident):
    """Empty list is how the orchestrator learns to escalate."""
    for action in (
        RemediationAction.RESET_CHAOS_FAULT,
        RemediationAction.RESTART_DEPLOYMENT,
    ):
        incident.attempts.append(executed_attempt(make_plan(action)))
    plans = engine.candidates(
        incident,
        hypothesis(RootCause.CHAOS_HTTP_FAULT, RemediationAction.RESET_CHAOS_FAULT),
        findings(),
    )
    assert plans == []
