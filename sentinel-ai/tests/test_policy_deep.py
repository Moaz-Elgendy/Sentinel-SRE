"""
Policy tests for Deep Investigation proposal eligibility
(`PolicyEngine.evaluate_deep_proposal`). Same spirit as test_policy.py: prove
each check is load-bearing by flipping exactly one field.

Critically, `allowed=True` here must never be confused with "may execute" —
these tests assert only ELIGIBILITY (for human authorization), matching the
verdict's own contract.
"""
from __future__ import annotations

from app.lifecycle.policy import PolicyEngine
from app.models.incident import (
    DeepActionTarget,
    DeepRemediationProposal,
    DenialReason,
    NovelActionType,
)


def _proposal(**overrides) -> DeepRemediationProposal:
    target = DeepActionTarget(
        namespace=overrides.pop("namespace", "citizen-portal"),
        deployment=overrides.pop("deployment", "citizen-service"),
        container="citizen-service",
        key="DATABASE_HOST",
        value="good-host",
        previous_value="bad-host",
        previous_value_existed=True,
    )
    defaults = dict(
        id="deep-test-0001",
        incident_id="INC-TEST-0001",
        created_at=0.0,
        problem="db host is wrong",
        root_cause="misconfigured env var",
        action_type=NovelActionType.SET_ENV_VAR,
        target=target,
        reason="observed bad host in env",
        expected_effect="db connects",
        risk_level="moderate",
        confidence=overrides.pop("confidence", 0.99),
    )
    defaults.update(overrides)
    return DeepRemediationProposal(**defaults)


def test_eligible_proposal_is_allowed_but_never_self_executing(policy_config, incident):
    engine = PolicyEngine(policy_config)
    proposal = _proposal()
    verdict = engine.evaluate_deep_proposal(incident, proposal)
    assert verdict.allowed is True
    assert "authoris" in verdict.detail.lower() or "authoriz" in verdict.detail.lower()


def test_target_must_match_incident_exactly(policy_config, incident):
    engine = PolicyEngine(policy_config)
    proposal = _proposal(namespace="some-other-namespace")
    verdict = engine.evaluate_deep_proposal(incident, proposal)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.BLAST_RADIUS_EXCEEDS_INCIDENT


def test_denied_deployment_is_never_eligible(policy_config, incident):
    engine = PolicyEngine(policy_config)
    incident.app = "citizen-postgres"
    proposal = _proposal(deployment="citizen-postgres")
    verdict = engine.evaluate_deep_proposal(incident, proposal)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.DEPLOYMENT_FROZEN_DENY


def test_low_confidence_is_never_eligible_even_though_it_would_pass_rollback_threshold(
    policy_config, incident
):
    """0.96 clears the ROLLBACK threshold (0.95) but must still fail the
    stricter deep-remediation threshold (0.97) - proves the two are
    independent gates, not a shared one."""
    engine = PolicyEngine(policy_config)
    proposal = _proposal(confidence=0.96)
    verdict = engine.evaluate_deep_proposal(incident, proposal)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.CONFIDENCE_TOO_LOW


def test_a_sensitive_looking_env_var_key_is_never_eligible(policy_config, incident):
    """Independent re-check of the same rule deep_investigation.
    apply_llm_response already applies at construction time — a proposal
    should never actually reach this method with such a key, but if one
    does (a future caller, a bug), policy must refuse it too."""
    engine = PolicyEngine(policy_config)
    proposal = _proposal(target=DeepActionTarget(
        namespace="citizen-portal", deployment="citizen-service", container="citizen-service",
        key="DB_PASSWORD", value="new-value", previous_value=None, previous_value_existed=False,
    ))
    verdict = engine.evaluate_deep_proposal(incident, proposal)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.SENSITIVE_ENV_VAR_KEY


def test_action_cap_is_shared_with_known_remediation(policy_config, incident):
    from app.models.incident import ActionParams, ActionPlan, AttemptRecord, RemediationAction, RemediationResult

    engine = PolicyEngine(policy_config)
    for _ in range(policy_config.max_actions_per_incident):
        plan = ActionPlan(
            action=RemediationAction.RESTART_DEPLOYMENT,
            params=ActionParams(namespace=incident.namespace, deployment=incident.app),
            confidence=0.9,
        )
        result = RemediationResult(action=plan.action, params=plan.params, succeeded=True)
        incident.attempts.append(AttemptRecord(plan=plan, result=result))

    proposal = _proposal()
    verdict = engine.evaluate_deep_proposal(incident, proposal)
    assert verdict.allowed is False
    assert verdict.reason is DenialReason.ACTION_CAP_REACHED
