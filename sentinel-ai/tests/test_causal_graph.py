"""
CAUSAL GRAPH tests.

Checks the three things that matter for an evidence-backed graph: (1) it
never fabricates a node/edge that isn't backed by something the incident
record actually holds, (2) it degrades gracefully at any lifecycle stage
(no evidence yet, no hypothesis yet, no attempts yet), and (3) it never
blurs a correlation into a fact or a hypothesis into a fact — the `kind`
field on every edge must stay honest.
"""
from __future__ import annotations

from app.lifecycle.causal_graph import build_causal_graph
from app.models.incident import (
    ActionParams,
    ActionPlan,
    AttemptRecord,
    DenialReason,
    Evidence,
    Hypothesis,
    Incident,
    PolicyVerdict,
    RemediationAction,
    RemediationResult,
    RootCause,
    Severity,
    ValidationOutcome,
    ValidationReport,
)


def make_bare_incident() -> Incident:
    return Incident(
        id="INC-TEST-0001",
        fingerprint="abc123",
        alertname="HighHTTPErrorRate",
        severity=Severity.CRITICAL,
        app="citizen-service",
        namespace="citizen-portal",
    )


def test_graph_on_a_freshly_detected_incident_has_only_the_alert_node():
    """No evidence, no hypothesis, no attempts yet — the graph must not
    invent any of them."""
    incident = make_bare_incident()
    graph = build_causal_graph(incident)
    assert len(graph["nodes"]) == 1
    assert graph["nodes"][0]["kind"] == "fact"
    assert graph["edges"] == []


def test_commit_and_deployment_are_linked_as_facts_not_correlations():
    incident = make_bare_incident()
    incident.evidence = Evidence(
        deploy_commit={"sha": "abcdef0123456789", "message": "bump db pool size"},
        replicaset_history=[
            {"revision": 7, "created_at": 100.0, "images": ["registry/citizen-service:abcdef0"]}
        ],
    )
    graph = build_causal_graph(incident)
    fact_edges = [e for e in graph["edges"] if e["kind"] == "fact"]
    assert any(
        e["label"] == "deployed as"
        and e["source"].startswith("commit:")
        and e["target"].startswith("deployment:")
        for e in fact_edges
    )


def test_correlations_are_kind_correlation_never_kind_fact():
    """`evidence.correlations` is lifecycle/correlation.py's own explicitly
    non-causal output. The graph must carry that through unchanged."""
    incident = make_bare_incident()
    incident.evidence = Evidence(
        error_rate=0.4,
        correlations=["error rate rose within 5 minutes of the latest deployment"],
    )
    graph = build_causal_graph(incident)
    correlation_edges = [e for e in graph["edges"] if e["kind"] == "correlation"]
    assert len(correlation_edges) >= 2  # evidence -> correlation node, correlation -> alert
    assert not any(
        "rose within 5 minutes" in n["label"] and n["kind"] != "correlation"
        for n in graph["nodes"]
    )


def test_hypothesis_reasoning_is_carried_verbatim_never_raw_chain_of_thought():
    incident = make_bare_incident()
    incident.evidence = Evidence(error_rate=0.4)
    incident.hypothesis = Hypothesis(
        root_cause=RootCause.BAD_DEPLOYMENT,
        confidence=0.97,
        reasoning="error rate spiked immediately after a new revision rolled out",
        recommended_action=RemediationAction.ROLLBACK_DEPLOYMENT,
        supporting=["deployment_revision changed", "error_rate_band changed"],
    )
    graph = build_causal_graph(incident)
    hyp_nodes = [n for n in graph["nodes"] if n["kind"] == "hypothesis"]
    assert len(hyp_nodes) == 1
    assert hyp_nodes[0]["detail"]["reasoning"] == incident.hypothesis.reasoning
    assert hyp_nodes[0]["detail"]["supporting"] == incident.hypothesis.supporting


def test_denied_action_has_no_outcome_node():
    """Policy denied it before execution, so nothing happened — the graph
    must not invent an outcome for something that never ran."""
    incident = make_bare_incident()
    incident.hypothesis = Hypothesis(
        root_cause=RootCause.BAD_DEPLOYMENT,
        confidence=0.6,
        reasoning="looks like a bad deployment but confidence is too low",
        recommended_action=RemediationAction.ROLLBACK_DEPLOYMENT,
    )
    plan = ActionPlan(
        action=RemediationAction.ROLLBACK_DEPLOYMENT,
        params=ActionParams(namespace="citizen-portal", deployment="citizen-service"),
        confidence=0.6,
    )
    verdict = PolicyVerdict(
        allowed=False,
        action=RemediationAction.ROLLBACK_DEPLOYMENT,
        reason=DenialReason.CONFIDENCE_TOO_LOW,
        detail="confidence 0.60 is below the 0.95 threshold for rollback",
    )
    incident.attempts.append(AttemptRecord(plan=plan, verdict=verdict, result=None))

    graph = build_causal_graph(incident)
    action_nodes = [n for n in graph["nodes"] if n["kind"] == "action"]
    outcome_nodes = [n for n in graph["nodes"] if n["kind"] == "outcome"]
    assert len(action_nodes) == 1
    assert action_nodes[0]["detail"]["allowed"] is False
    assert action_nodes[0]["detail"]["denial_reason"] == "confidence_too_low"
    assert outcome_nodes == []


def test_executed_and_validated_action_produces_a_full_action_outcome_chain():
    incident = make_bare_incident()
    incident.hypothesis = Hypothesis(
        root_cause=RootCause.BAD_DEPLOYMENT,
        confidence=0.97,
        reasoning="new revision correlated with the error spike",
        recommended_action=RemediationAction.ROLLBACK_DEPLOYMENT,
    )
    plan = ActionPlan(
        action=RemediationAction.ROLLBACK_DEPLOYMENT,
        params=ActionParams(namespace="citizen-portal", deployment="citizen-service"),
        confidence=0.97,
    )
    verdict = PolicyVerdict(allowed=True, action=RemediationAction.ROLLBACK_DEPLOYMENT)
    result = RemediationResult(
        action=RemediationAction.ROLLBACK_DEPLOYMENT,
        params=plan.params,
        succeeded=True,
        detail="rolled back to revision 6",
    )
    validation = ValidationReport(outcome=ValidationOutcome.PASSED, detail="error rate recovered")
    incident.attempts.append(
        AttemptRecord(plan=plan, verdict=verdict, result=result, validation=validation)
    )

    graph = build_causal_graph(incident)
    action_edges = [e for e in graph["edges"] if e["kind"] == "action" and e["label"] == "executed"]
    assert len(action_edges) == 1
    outcome_nodes = [n for n in graph["nodes"] if n["kind"] == "outcome"]
    assert outcome_nodes[0]["detail"]["succeeded"] is True
    assert outcome_nodes[0]["detail"]["validation_outcome"] == "passed"


def test_graph_is_deterministic_for_the_same_incident():
    incident = make_bare_incident()
    incident.evidence = Evidence(error_rate=0.3, correlations=["a"])
    first = build_causal_graph(incident)
    second = build_causal_graph(incident)
    assert first == second
