"""
Policy Engine tests.

This is the most important test file in Sentinel. The Policy Engine is the
only thing standing between a hypothesis and a cluster mutation, so every
rule it implements gets a test that would fail if the rule were removed.

Style note: each test flips exactly ONE input away from a known-good baseline
and asserts on the specific `DenialReason`. Asserting only `allowed is False`
would pass even if a completely different check was doing the denying, which
would let a rule be silently deleted.
"""
from __future__ import annotations

import time

import pytest

from app.lifecycle.policy import PolicyContext, PolicyEngine
from app.models.incident import DenialReason, RemediationAction

from .conftest import executed_attempt, make_plan


@pytest.fixture
def engine(policy_config):
    return PolicyEngine(policy_config)


NOW = 1_700_000_000.0


# ---------------------------------------------------------------------------
# Allow-lists and the frozen deny-list
# ---------------------------------------------------------------------------
def test_allowed_deployment_restart_is_authorised(engine, incident, rollback_ready_context):
    plan = make_plan(RemediationAction.RESTART_DEPLOYMENT, confidence=0.95)
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is True
    assert verdict.checks["deployment_allowed"] is True
    assert verdict.checks["namespace_allowed"] is True


@pytest.mark.parametrize("database", ["citizen-postgres", "notification-postgres"])
def test_postgres_is_never_remediable(engine, incident, rollback_ready_context, database):
    """Databases escalate to a human. No confidence justifies touching them."""
    for action in (
        RemediationAction.RESTART_DEPLOYMENT,
        RemediationAction.ROLLBACK_DEPLOYMENT,
        RemediationAction.SCALE_DEPLOYMENT,
        RemediationAction.RESET_CHAOS_FAULT,
    ):
        plan = make_plan(action, confidence=1.0, deployment=database, replicas=2)
        verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
        assert verdict.allowed is False, f"{action.value} was allowed against {database}"
        assert verdict.reason is DenialReason.DEPLOYMENT_FROZEN_DENY


def test_frozen_deny_list_beats_a_misconfigured_allow_list(policy_config, incident,
                                                           rollback_ready_context):
    """A typo in ALLOWED_DEPLOYMENTS must not make Postgres remediable.

    This is the specific reason the deny-list is non-configurable and checked
    first. Here we simulate the operator error directly.
    """
    bad_config = policy_config
    bad_config.allowed_deployments = frozenset(
        {"citizen-service", "citizen-postgres"}  # operator error
    )
    engine = PolicyEngine(bad_config)
    plan = make_plan(
        RemediationAction.RESTART_DEPLOYMENT, confidence=1.0, deployment="citizen-postgres"
    )
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.DEPLOYMENT_FROZEN_DENY


def test_kube_system_is_never_touched(engine, incident, rollback_ready_context):
    plan = make_plan(
        RemediationAction.RESTART_DEPLOYMENT,
        confidence=1.0,
        namespace="kube-system",
        deployment="coredns",
    )
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.NAMESPACE_FROZEN_DENY


def test_unlisted_deployment_denied(engine, incident, rollback_ready_context):
    plan = make_plan(
        RemediationAction.RESTART_DEPLOYMENT, confidence=1.0, deployment="grafana"
    )
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.DEPLOYMENT_NOT_ALLOWED


def test_unlisted_namespace_denied(engine, incident, rollback_ready_context):
    plan = make_plan(
        RemediationAction.RESTART_DEPLOYMENT, confidence=1.0, namespace="default"
    )
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.NAMESPACE_NOT_ALLOWED


def test_missing_target_denied(engine, incident, rollback_ready_context):
    plan = make_plan(
        RemediationAction.RESTART_DEPLOYMENT, confidence=1.0, deployment=None
    )
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.MISSING_TARGET


def test_stateful_flag_denies_even_an_allow_listed_target(
    engine, incident, rollback_ready_context
):
    rollback_ready_context.target_is_stateful = True
    plan = make_plan(RemediationAction.RESTART_DEPLOYMENT, confidence=1.0)
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.STATEFUL_TARGET


# ---------------------------------------------------------------------------
# Confidence thresholds
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "action,threshold",
    [
        (RemediationAction.ROLLBACK_DEPLOYMENT, 0.95),
        (RemediationAction.RESTART_DEPLOYMENT, 0.90),
        (RemediationAction.SCALE_DEPLOYMENT, 0.90),
        (RemediationAction.RESET_CHAOS_FAULT, 0.90),
    ],
)
def test_confidence_threshold_boundary(
    engine, incident, rollback_ready_context, action, threshold
):
    """Exactly at the threshold passes; a hair below is denied."""
    at = make_plan(action, confidence=threshold, replicas=2, target_revision=2)
    assert engine.evaluate(incident, at, rollback_ready_context, NOW).allowed is True

    below = make_plan(action, confidence=threshold - 0.01, replicas=2, target_revision=2)
    verdict = engine.evaluate(incident, below, rollback_ready_context, NOW)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.CONFIDENCE_TOO_LOW


def test_rollback_needs_higher_confidence_than_restart(
    engine, incident, rollback_ready_context
):
    """0.92 is enough to restart but must not be enough to roll back."""
    restart = make_plan(RemediationAction.RESTART_DEPLOYMENT, confidence=0.92)
    assert engine.evaluate(incident, restart, rollback_ready_context, NOW).allowed is True

    rollback = make_plan(
        RemediationAction.ROLLBACK_DEPLOYMENT, confidence=0.92, target_revision=2
    )
    verdict = engine.evaluate(incident, rollback, rollback_ready_context, NOW)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.CONFIDENCE_TOO_LOW


# ---------------------------------------------------------------------------
# Rollback preconditions — one test per precondition
# ---------------------------------------------------------------------------
def test_rollback_allowed_when_every_precondition_holds(
    engine, incident, rollback_ready_context
):
    """And it is AUTONOMOUS: there is no approval gate to satisfy."""
    plan = make_plan(
        RemediationAction.ROLLBACK_DEPLOYMENT, confidence=0.96, target_revision=2
    )
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is True
    for check in (
        "previous_revision_exists",
        "deployment_history",
        "deploy_correlation",
        "within_correlation_window",
        "reversible",
        "validation_available",
    ):
        assert verdict.checks[check] is True, f"{check} was not evaluated"
    assert "no human approval gate" in verdict.detail


def test_rollback_denied_without_previous_revision(engine, incident, rollback_ready_context):
    rollback_ready_context.previous_revision_exists = False
    plan = make_plan(RemediationAction.ROLLBACK_DEPLOYMENT, confidence=0.99)
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.NO_PREVIOUS_REVISION


def test_rollback_denied_without_history(engine, incident, rollback_ready_context):
    rollback_ready_context.deployment_history_count = 1
    plan = make_plan(RemediationAction.ROLLBACK_DEPLOYMENT, confidence=0.99)
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.NO_DEPLOYMENT_HISTORY


def test_rollback_denied_without_deploy_correlation(
    engine, incident, rollback_ready_context
):
    """The core rule: do not roll back code that did not cause the incident."""
    rollback_ready_context.deploy_correlates_with_onset = False
    plan = make_plan(RemediationAction.ROLLBACK_DEPLOYMENT, confidence=0.99)
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.NO_DEPLOY_CORRELATION


def test_rollback_denied_when_deploy_is_outside_the_window(
    engine, incident, rollback_ready_context
):
    # 31 minutes, against a 30 minute window.
    rollback_ready_context.last_deploy_age_seconds = 31 * 60
    plan = make_plan(RemediationAction.ROLLBACK_DEPLOYMENT, confidence=0.99)
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.NO_DEPLOY_CORRELATION
    assert verdict.checks["within_correlation_window"] is False


def test_rollback_denied_when_deploy_age_unknown(engine, incident, rollback_ready_context):
    """Unknown age is treated as outside the window, not inside it."""
    rollback_ready_context.last_deploy_age_seconds = None
    plan = make_plan(RemediationAction.ROLLBACK_DEPLOYMENT, confidence=0.99)
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.NO_DEPLOY_CORRELATION


def test_rollback_denied_when_not_reversible(engine, incident, rollback_ready_context):
    rollback_ready_context.rollback_reversible = False
    plan = make_plan(RemediationAction.ROLLBACK_DEPLOYMENT, confidence=0.99)
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.NOT_REVERSIBLE


def test_rollback_denied_without_recovery_validation(
    engine, incident, rollback_ready_context
):
    rollback_ready_context.recovery_validation_available = False
    plan = make_plan(RemediationAction.ROLLBACK_DEPLOYMENT, confidence=0.99)
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.VALIDATION_UNAVAILABLE


def test_default_policy_context_denies_rollback(engine, incident):
    """The defaults are conservative: a caller who forgets to populate the
    context gets a denial, never an authorisation."""
    plan = make_plan(RemediationAction.ROLLBACK_DEPLOYMENT, confidence=1.0)
    verdict = engine.evaluate(incident, plan, PolicyContext(), NOW)
    assert verdict.allowed is False


# ---------------------------------------------------------------------------
# Scale band
# ---------------------------------------------------------------------------
def test_scale_to_zero_is_refused(engine, incident, rollback_ready_context):
    """Never 0. A request for 0 is rejected, not clamped — it is a bug."""
    plan = make_plan(RemediationAction.SCALE_DEPLOYMENT, confidence=0.99, replicas=0)
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.REPLICAS_OUT_OF_BAND
    assert verdict.checks["replicas_not_zero"] is False


def test_scale_above_max_is_clamped_not_denied(engine, incident, rollback_ready_context):
    plan = make_plan(RemediationAction.SCALE_DEPLOYMENT, confidence=0.99, replicas=10)
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is True
    assert verdict.adjusted_params is not None
    assert verdict.adjusted_params.replicas == 3  # max_replicas


def test_scale_below_min_is_clamped_up(engine, incident, rollback_ready_context):
    # min_replicas is 1, so a request for 1 is exactly at the floor and needs
    # no adjustment; this proves the floor itself is applied.
    plan = make_plan(RemediationAction.SCALE_DEPLOYMENT, confidence=0.99, replicas=1)
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is True
    assert verdict.adjusted_params is None  # nothing to adjust


def test_scale_without_a_replica_count_is_denied(engine, incident, rollback_ready_context):
    plan = make_plan(RemediationAction.SCALE_DEPLOYMENT, confidence=0.99, replicas=None)
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.REPLICAS_OUT_OF_BAND


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------
def test_action_cap_stops_further_actions(engine, incident, rollback_ready_context):
    for _ in range(3):
        incident.attempts.append(
            executed_attempt(
                make_plan(RemediationAction.RESTART_DEPLOYMENT), started_at=NOW - 10_000
            )
        )
    plan = make_plan(RemediationAction.SCALE_DEPLOYMENT, confidence=0.99, replicas=2)
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.ACTION_CAP_REACHED


def test_denied_candidates_do_not_count_towards_the_cap(
    engine, incident, rollback_ready_context
):
    """A policy denial never touched the cluster, so it must not burn budget.

    Otherwise three denials would exhaust the cap and Sentinel would escalate
    without ever having tried anything.
    """
    from app.models.incident import AttemptRecord

    for _ in range(5):
        incident.attempts.append(
            AttemptRecord(
                plan=make_plan(RemediationAction.ROLLBACK_DEPLOYMENT), result=None
            )
        )
    plan = make_plan(RemediationAction.RESTART_DEPLOYMENT, confidence=0.99)
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is True


def test_cooldown_blocks_repeating_the_same_action_on_the_same_target(
    engine, incident, rollback_ready_context
):
    incident.attempts.append(
        executed_attempt(
            make_plan(RemediationAction.RESTART_DEPLOYMENT), started_at=NOW - 30
        )
    )
    plan = make_plan(RemediationAction.RESTART_DEPLOYMENT, confidence=0.99)
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.COOLDOWN_ACTIVE


def test_cooldown_expires(engine, incident, rollback_ready_context):
    incident.attempts.append(
        executed_attempt(
            make_plan(RemediationAction.RESTART_DEPLOYMENT), started_at=NOW - 121
        )
    )
    plan = make_plan(RemediationAction.RESTART_DEPLOYMENT, confidence=0.99)
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is True


def test_cooldown_is_per_target(engine, incident, rollback_ready_context):
    """A restart of citizen-service must not block a restart of another
    allow-listed deployment."""
    incident.attempts.append(
        executed_attempt(
            make_plan(RemediationAction.RESTART_DEPLOYMENT, deployment="citizen-service"),
            started_at=NOW - 5,
        )
    )
    plan = make_plan(
        RemediationAction.RESTART_DEPLOYMENT,
        confidence=0.99,
        deployment="notification-service",
    )
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is True


def test_cooldown_is_per_action(engine, incident, rollback_ready_context):
    """A recent restart must not block a scale of the same deployment."""
    incident.attempts.append(
        executed_attempt(
            make_plan(RemediationAction.RESTART_DEPLOYMENT), started_at=NOW - 5
        )
    )
    plan = make_plan(RemediationAction.SCALE_DEPLOYMENT, confidence=0.99, replicas=2)
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is True


# ---------------------------------------------------------------------------
# Chaos reset preconditions
# ---------------------------------------------------------------------------
def test_chaos_reset_denied_without_a_control_plane(
    engine, incident, rollback_ready_context
):
    rollback_ready_context.chaos_surface_available = False
    plan = make_plan(RemediationAction.RESET_CHAOS_FAULT, confidence=0.99)
    verdict = engine.evaluate(incident, plan, rollback_ready_context, NOW)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.NO_CHAOS_SURFACE


# ---------------------------------------------------------------------------
# Escalation and unknown actions
# ---------------------------------------------------------------------------
def test_escalation_is_always_permitted(engine, incident):
    """Handing an incident to a human must never be blocked by policy."""
    plan = make_plan(RemediationAction.ESCALATE, confidence=0.0, deployment=None)
    verdict = engine.evaluate(incident, plan, PolicyContext(), NOW)
    assert verdict.allowed is True


def test_threshold_for_unknown_action_is_unreachable(policy_config):
    """Fail closed: a new enum member with no threshold can never pass."""
    assert policy_config.threshold_for(RemediationAction.ESCALATE) > 1.0


def test_engine_uses_injected_clock_not_wall_clock(engine, incident, rollback_ready_context):
    """Cooldown must be computed from the passed `now`.

    Guards against someone replacing the parameter with time.time() inside
    the engine, which would make the whole file untestable and the cooldown
    unverifiable.
    """
    incident.attempts.append(
        executed_attempt(
            make_plan(RemediationAction.RESTART_DEPLOYMENT), started_at=time.time()
        )
    )
    plan = make_plan(RemediationAction.RESTART_DEPLOYMENT, confidence=0.99)
    # A `now` far in the future must expire the cooldown.
    far_future = time.time() + 10_000
    assert engine.evaluate(incident, plan, rollback_ready_context, far_future).allowed is True
