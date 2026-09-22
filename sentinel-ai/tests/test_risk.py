"""
RISK / IMPACT ASSESSMENT tests (lifecycle/risk.py).

Style matches test_policy.py: flip one input at a time and assert on the
specific field it should move, so a check can't be silently deleted while
"a risk assessment exists" stays trivially true.
"""
from __future__ import annotations

from app.lifecycle.risk import (
    BEYOND_INCIDENT_SCOPE,
    SINGLE_WORKLOAD,
    assess_risk,
    blast_radius_scope,
)
from app.models.incident import RemediationAction

from .conftest import make_plan


def test_default_plan_matches_the_incident_its_own_scope(incident):
    """conftest's make_plan() defaults to the same namespace/deployment as
    the `incident` fixture — exactly what decision.py always produces."""
    plan = make_plan(RemediationAction.RESTART_DEPLOYMENT)
    assert blast_radius_scope(incident, plan) == SINGLE_WORKLOAD


def test_plan_targeting_a_different_namespace_is_beyond_incident_scope(incident):
    plan = make_plan(RemediationAction.RESTART_DEPLOYMENT, namespace="some-other-namespace")
    assert blast_radius_scope(incident, plan) == BEYOND_INCIDENT_SCOPE


def test_plan_targeting_a_different_deployment_is_beyond_incident_scope(incident):
    plan = make_plan(RemediationAction.RESTART_DEPLOYMENT, deployment="some-other-service")
    assert blast_radius_scope(incident, plan) == BEYOND_INCIDENT_SCOPE


def test_beyond_incident_scope_is_always_high_regardless_of_other_factors(incident):
    plan = make_plan(RemediationAction.RESTART_DEPLOYMENT, namespace="some-other-namespace")
    risk = assess_risk(
        incident,
        plan,
        candidate_index=0,
        rollback_reversible=True,
        recovery_validation_available=True,
    )
    assert risk.level == "high"
    assert risk.blast_radius_scope == BEYOND_INCIDENT_SCOPE


def test_restart_is_low_impact_when_reversible_and_validated(incident):
    plan = make_plan(RemediationAction.RESTART_DEPLOYMENT)
    risk = assess_risk(
        incident,
        plan,
        candidate_index=0,
        rollback_reversible=False,  # irrelevant for restart
        recovery_validation_available=True,
    )
    assert risk.level == "low"
    assert risk.reversible is True
    assert risk.validation_available is True


def test_restart_drops_to_moderate_when_validation_is_unavailable(incident):
    plan = make_plan(RemediationAction.RESTART_DEPLOYMENT)
    risk = assess_risk(
        incident,
        plan,
        candidate_index=0,
        rollback_reversible=False,
        recovery_validation_available=False,
    )
    assert risk.level == "moderate"


def test_rollback_is_never_classified_low_even_when_fully_favourable(incident):
    """A rollback changes what code is running — that alone keeps it out of
    the "low impact" bucket regardless of reversibility/validation."""
    plan = make_plan(RemediationAction.ROLLBACK_DEPLOYMENT, target_revision=6)
    risk = assess_risk(
        incident,
        plan,
        candidate_index=0,
        rollback_reversible=True,
        recovery_validation_available=True,
    )
    assert risk.level == "moderate"
    assert risk.reversible is True


def test_rollback_that_is_not_reversible_is_high(incident):
    plan = make_plan(RemediationAction.ROLLBACK_DEPLOYMENT, target_revision=6)
    risk = assess_risk(
        incident,
        plan,
        candidate_index=0,
        rollback_reversible=False,
        recovery_validation_available=True,
    )
    assert risk.level == "high"
    assert risk.reversible is False


def test_least_impactful_untried_option_is_only_true_at_index_zero(incident):
    plan = make_plan(RemediationAction.RESTART_DEPLOYMENT)
    first = assess_risk(
        incident, plan, candidate_index=0, rollback_reversible=True, recovery_validation_available=True
    )
    second = assess_risk(
        incident, plan, candidate_index=1, rollback_reversible=True, recovery_validation_available=True
    )
    assert first.least_impactful_untried_option is True
    assert second.least_impactful_untried_option is False


def test_to_dict_is_a_plain_json_serialisable_shape(incident):
    plan = make_plan(RemediationAction.SCALE_DEPLOYMENT, replicas=2)
    risk = assess_risk(
        incident, plan, candidate_index=0, rollback_reversible=False, recovery_validation_available=True
    )
    data = risk.to_dict()
    assert data["action"] == "scale_deployment"
    assert data["affected_namespace"] == incident.namespace
    assert data["affected_deployment"] == incident.target_deployment
    assert isinstance(data["reasoning"], list) and len(data["reasoning"]) > 0
