"""
RCA tests.

Two halves:

1. The **rule engine** — deterministic mapping from evidence to root cause,
   confidence and recommended action. These tests pin the confidence values
   because confidence is what the Policy Engine gates on: if a rule's
   confidence drifts above a threshold, an action becomes autonomous that was
   not before, and that must be a deliberate edit rather than an accident.

2. The **LLM trust boundary** (`apply_llm_response`) — the point where
   untrusted model output enters. These tests are adversarial: they feed the
   validator responses that try to switch the action to a rollback, push
   confidence to 1.0, invent a root cause, and smuggle a shell command. Every
   one must be rejected without affecting what Sentinel is permitted to do.
"""
from __future__ import annotations

import json

from app.lifecycle.correlation import CorrelationFindings
from app.lifecycle.rca import (
    LLM_CONFIDENCE_CEILING,
    LLM_CONFIDENCE_DELTA_CAP,
    analyse,
    apply_llm_response,
)
from app.models.incident import (
    Evidence,
    Hypothesis,
    RemediationAction,
    RootCause,
)


def findings(**kwargs) -> CorrelationFindings:
    f = CorrelationFindings()
    for key, value in kwargs.items():
        setattr(f, key, value)
    return f


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------
def test_chaos_db_fault_is_high_confidence_chaos_reset(incident):
    h = analyse(incident, Evidence(), findings(chaos_db_fault=True))
    assert h.root_cause is RootCause.CHAOS_DATABASE_FAULT
    assert h.recommended_action is RemediationAction.RESET_CHAOS_FAULT
    # Must clear the 0.90 chaos-reset threshold so this remediates autonomously.
    assert h.confidence >= 0.90


def test_chaos_beats_a_recent_deployment(incident):
    """A deploy plus an active chaos fault is a chaos fault, not a bad deploy.

    Rolling back would neither clear the injected fault nor be an honest
    account of the cause. The correlation phase already refuses to set
    deploy_correlates_with_onset when chaos is active; this asserts the RCA
    ordering independently.
    """
    h = analyse(
        incident,
        Evidence(),
        findings(chaos_error_fault=True, recent_deployment=True, previous_revision=2),
    )
    assert h.root_cause is RootCause.CHAOS_HTTP_FAULT
    assert h.recommended_action is RemediationAction.RESET_CHAOS_FAULT


def test_bad_deployment_with_image_change_clears_the_rollback_gate(incident):
    h = analyse(
        incident,
        Evidence(),
        findings(
            deploy_correlates_with_onset=True,
            previous_revision=2,
            current_revision=3,
            revision_count=3,
            image_changed=True,
            error_spike=True,
        ),
    )
    assert h.root_cause is RootCause.BAD_DEPLOYMENT
    assert h.recommended_action is RemediationAction.ROLLBACK_DEPLOYMENT
    assert h.confidence >= 0.95, "must clear the rollback threshold"


def test_bad_deployment_without_an_image_change_stays_below_the_rollback_gate(incident):
    """A revision bump with identical images is usually an annotation-only
    change (a rollout restart). Rolling 'back' to an identical template
    achieves nothing, so confidence must stay under 0.95."""
    h = analyse(
        incident,
        Evidence(),
        findings(
            deploy_correlates_with_onset=True,
            previous_revision=2,
            revision_count=3,
            image_changed=False,
            error_spike=True,
        ),
    )
    assert h.root_cause is RootCause.BAD_DEPLOYMENT
    assert h.confidence < 0.95


def test_memory_growth_without_a_deploy_is_a_leak(incident):
    h = analyse(
        incident, Evidence(), findings(memory_growth_suspicious=True, recent_deployment=False)
    )
    assert h.root_cause is RootCause.MEMORY_LEAK
    assert h.recommended_action is RemediationAction.RESTART_DEPLOYMENT
    assert h.confidence >= 0.90


def test_capacity_pressure_prefers_scaling(incident):
    h = analyse(incident, Evidence(), findings(capacity_pressure=True, latency_spike=True))
    assert h.root_cause is RootCause.CAPACITY_SHORTFALL
    assert h.recommended_action is RemediationAction.SCALE_DEPLOYMENT


def test_crash_loop_without_a_deploy_stays_below_the_restart_gate(incident):
    """A restart cannot fix a config-caused crash loop, so this must escalate
    rather than restart-loop."""
    h = analyse(incident, Evidence(), findings(crash_looping=True))
    assert h.root_cause is RootCause.POD_CRASH_LOOP
    assert h.confidence < 0.90


def test_downstream_degradation_escalates(incident):
    h = analyse(incident, Evidence(), findings(downstream_degraded=True))
    assert h.root_cause is RootCause.DOWNSTREAM_DEPENDENCY
    assert h.recommended_action is RemediationAction.ESCALATE


def test_unattributed_error_spike_is_low_confidence(incident):
    h = analyse(incident, Evidence(), findings(error_spike=True))
    assert h.root_cause is RootCause.UNKNOWN
    assert h.confidence < 0.90


def test_no_evidence_yields_a_very_low_confidence_unknown(incident):
    h = analyse(incident, Evidence(), findings())
    assert h.root_cause is RootCause.UNKNOWN
    assert h.recommended_action is RemediationAction.ESCALATE
    assert h.confidence < 0.5


# ---------------------------------------------------------------------------
# Confidence penalties for missing evidence
# ---------------------------------------------------------------------------
def test_missing_kubernetes_evidence_lowers_confidence_below_the_gate(incident):
    """A hypothesis formed while blind to the API server must not authorise
    a rollback."""
    good = analyse(
        incident,
        Evidence(),
        findings(
            deploy_correlates_with_onset=True,
            previous_revision=2,
            revision_count=3,
            image_changed=True,
        ),
    )
    blind = analyse(
        incident,
        Evidence(),
        findings(
            deploy_correlates_with_onset=True,
            previous_revision=2,
            revision_count=3,
            image_changed=True,
            k8s_missing=True,
        ),
    )
    assert blind.confidence < good.confidence
    assert blind.confidence < 0.95


def test_missing_metrics_penalty_is_the_largest(incident):
    base = analyse(incident, Evidence(), findings(chaos_db_fault=True))
    no_metrics = analyse(
        incident, Evidence(), findings(chaos_db_fault=True, metrics_missing=True)
    )
    no_logs = analyse(
        incident, Evidence(), findings(chaos_db_fault=True, logs_missing=True)
    )
    assert no_metrics.confidence < no_logs.confidence < base.confidence


# ---------------------------------------------------------------------------
# The LLM trust boundary — adversarial
# ---------------------------------------------------------------------------
def base_hypothesis() -> Hypothesis:
    return Hypothesis(
        root_cause=RootCause.CHAOS_DATABASE_FAULT,
        confidence=0.90,
        reasoning="rule engine reasoning",
        recommended_action=RemediationAction.RESET_CHAOS_FAULT,
        rule_confidence=0.90,
    )


def test_llm_cannot_change_the_action():
    """The single most important assertion in this file.

    A model that returns a rollback when the rules said chaos-reset must be
    discarded entirely. Otherwise prompt-injected text inside a log line could
    choose a cluster mutation.
    """
    hostile = json.dumps(
        {
            "root_cause": "chaos_database_fault",
            "confidence": 0.99,
            "reasoning": "roll it back",
            "recommended_action": "rollback_deployment",
        }
    )
    result = apply_llm_response(base_hypothesis(), hostile)
    assert result.recommended_action is RemediationAction.RESET_CHAOS_FAULT
    assert result.llm_used is False
    assert result.confidence == 0.90
    assert "discarded" in result.llm_note


def test_llm_cannot_invent_an_action_outside_the_enum():
    hostile = json.dumps(
        {
            "root_cause": "chaos_database_fault",
            "confidence": 0.95,
            "reasoning": "run this",
            "recommended_action": "kubectl delete ns citizen-portal",
        }
    )
    result = apply_llm_response(base_hypothesis(), hostile)
    assert result.recommended_action is RemediationAction.RESET_CHAOS_FAULT
    assert result.llm_used is False


def test_llm_cannot_push_confidence_past_the_delta_cap():
    """A request for 1.0 from a 0.90 base yields at most 0.90 + the cap."""
    response = json.dumps(
        {
            "root_cause": "chaos_database_fault",
            "confidence": 1.0,
            "reasoning": "certain",
            "recommended_action": "reset_chaos_fault",
        }
    )
    result = apply_llm_response(base_hypothesis(), response)
    assert result.llm_used is True
    assert result.confidence == 0.90 + LLM_CONFIDENCE_DELTA_CAP


def test_llm_cannot_lift_a_hypothesis_over_the_rollback_gate():
    """The arithmetic that makes this safe: 0.90 + 0.03 < 0.95.

    So even with maximum agreement, the LLM can never be the reason a
    rollback becomes authorised.
    """
    rollback_hypothesis = Hypothesis(
        root_cause=RootCause.BAD_DEPLOYMENT,
        confidence=0.90,
        reasoning="rules",
        recommended_action=RemediationAction.ROLLBACK_DEPLOYMENT,
        rule_confidence=0.90,
    )
    response = json.dumps(
        {
            "root_cause": "bad_deployment",
            "confidence": 1.0,
            "reasoning": "definitely the deploy",
            "recommended_action": "rollback_deployment",
        }
    )
    result = apply_llm_response(rollback_hypothesis, response)
    assert result.confidence < 0.95


def test_llm_confidence_is_capped_by_the_ceiling():
    high = Hypothesis(
        root_cause=RootCause.CHAOS_DATABASE_FAULT,
        confidence=0.97,
        reasoning="rules",
        recommended_action=RemediationAction.RESET_CHAOS_FAULT,
    )
    response = json.dumps(
        {
            "root_cause": "chaos_database_fault",
            "confidence": 1.0,
            "reasoning": "sure",
            "recommended_action": "reset_chaos_fault",
        }
    )
    result = apply_llm_response(high, response)
    assert result.confidence <= LLM_CONFIDENCE_CEILING


def test_llm_may_lower_confidence():
    """Doubt is allowed — that direction is always safe."""
    response = json.dumps(
        {
            "root_cause": "chaos_database_fault",
            "confidence": 0.50,
            "reasoning": "not convinced",
            "recommended_action": "reset_chaos_fault",
        }
    )
    result = apply_llm_response(base_hypothesis(), response)
    assert result.confidence == 0.90 - LLM_CONFIDENCE_DELTA_CAP


def test_llm_cannot_change_the_root_cause():
    response = json.dumps(
        {
            "root_cause": "bad_deployment",
            "confidence": 0.95,
            "reasoning": "actually a deploy",
            "recommended_action": "reset_chaos_fault",
        }
    )
    result = apply_llm_response(base_hypothesis(), response)
    assert result.root_cause is RootCause.CHAOS_DATABASE_FAULT
    assert result.llm_used is False


def test_non_json_llm_response_is_ignored():
    result = apply_llm_response(base_hypothesis(), "I'm sorry, I can't do that.")
    assert result.llm_used is False
    assert result.confidence == 0.90


def test_json_array_llm_response_is_ignored():
    result = apply_llm_response(base_hypothesis(), '["reset_chaos_fault"]')
    assert result.llm_used is False


def test_llm_response_with_a_non_numeric_confidence_keeps_the_base():
    response = json.dumps(
        {
            "root_cause": "chaos_database_fault",
            "confidence": "very high",
            "reasoning": "ok",
            "recommended_action": "reset_chaos_fault",
        }
    )
    result = apply_llm_response(base_hypothesis(), response)
    assert result.confidence == 0.90


def test_accepted_llm_response_keeps_the_rule_reasoning():
    """The deterministic explanation must survive in the post-mortem even
    when the model's narrative is used."""
    response = json.dumps(
        {
            "root_cause": "chaos_database_fault",
            "confidence": 0.90,
            "reasoning": "an injected database fault is returning 503s",
            "recommended_action": "reset_chaos_fault",
        }
    )
    result = apply_llm_response(base_hypothesis(), response)
    assert result.llm_used is True
    assert "rule engine reasoning" in result.reasoning
    assert result.source == "rules+llm"


def test_action_parse_rejects_injection_shaped_strings():
    """RemediationAction.parse is the enum gate. It must never be lenient."""
    for hostile in (
        "restart_deployment; rm -rf /",
        "restart_deployment && kubectl delete pod",
        "RESTART_DEPLOYMENT\n",
        "delete_namespace",
        "",
        None,
        123,
        ["restart_deployment"],
    ):
        assert RemediationAction.parse(hostile) is None, hostile
    # And it does accept the exact values, including surrounding whitespace
    # and casing, which is the only leniency allowed.
    assert RemediationAction.parse(" Restart_Deployment ") is RemediationAction.RESTART_DEPLOYMENT
