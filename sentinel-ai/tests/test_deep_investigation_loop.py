"""
Regression test for the bounded, iterative Deep Investigation loop
(`investigate_deep` in lifecycle/deep_investigation.py), using the exact
production incident this feature generalises from: citizen-service left
running a placeholder/invalid image one revision back from a real, valid
one — the same shape of bug behind this repo's own Init:InvalidImageName
incident (see kubernetes_client.find_previous_revision's own docstring,
which names this scenario explicitly).

Two things must both be true for this to be safe rather than merely
convenient:

1. When the model calls a real tool (`inspect_previous_revision`, backed by
   the SAME `find_previous_revision` logic RemediationEngine itself uses at
   execution time — not a re-derived approximation) and then proposes
   `update_container_image` using the image THAT TOOL surfaced, the proposal
   must be accepted, carry that real, evidence-grounded image, and record
   the tool call in the investigation's audit trace.
2. When a (compromised, hallucinating, or merely wrong) model proposes an
   image it never saw from any tool or evidence — including one that merely
   LOOKS plausible — the proposal must be rejected outright. Evidence-
   grounding is enforced in code (`_build_image_target`), never by trusting
   the model to only ever ask for real things.
"""
from __future__ import annotations

import json

import pytest

from app.lifecycle.deep_investigation import investigate_deep
from app.lifecycle.deep_investigation_tools import ToolContext
from app.models.incident import (
    DeepInvestigationTrigger,
    Evidence,
    Hypothesis,
    Incident,
    NovelActionType,
    RemediationAction,
    RootCause,
    Severity,
)
from app.reasoning.base import Reasoner
from tests.conftest import FakeKubernetes

REAL_IMAGE = (
    "890608336467.dkr.ecr.eu-central-1.amazonaws.com/sentinel-sre-demo/"
    "citizen-service:6e36eedd0da47e4a24f0aa5a1c534ce6f4954a84"
)
PLACEHOLDER_IMAGE = (
    "890608336467.dkr.ecr.eu-central-1.amazonaws.com/sentinel-sre-demo/"
    "citizen-service:PLACEHOLDER"
)
HALLUCINATED_IMAGE = (
    "890608336467.dkr.ecr.eu-central-1.amazonaws.com/sentinel-sre-demo/"
    "citizen-service:deadbeefcafefeed0000000000000000000000"
)


class ScriptedReasoner(Reasoner):
    """A Reasoner whose `complete_json` returns one pre-written JSON turn per
    call, in order — a deterministic stand-in for the real model so this
    test exercises `investigate_deep`'s own loop, tool dispatch and
    trust-boundary logic, not any particular LLM's behaviour."""

    label = "test:scripted"

    def __init__(self, turns: list[dict]):
        self._turns = list(turns)
        self.calls = 0
        self.user_prompts: list[str] = []

    async def complete_json(self, system_prompt: str, user_prompt: str) -> str | None:
        self.calls += 1
        self.user_prompts.append(user_prompt)
        if not self._turns:
            return json.dumps({"action": "no_safe_fix", "reason": "script exhausted"})
        return json.dumps(self._turns.pop(0))


def _bad_deployment_incident_and_evidence():
    incident = Incident(
        id="INC-TEST-BADDEPLOY",
        fingerprint="bad-deploy-fp",
        alertname="HighHTTPErrorRate",
        severity=Severity.CRITICAL,
        app="citizen-service",
        namespace="citizen-portal",
    )
    replicasets = [
        {"revision": 3, "images": [PLACEHOLDER_IMAGE], "images_valid": False, "created_at": 200.0},
        {"revision": 2, "images": [REAL_IMAGE], "images_valid": True, "created_at": 100.0},
        {"revision": 1, "images": [REAL_IMAGE], "images_valid": True, "created_at": 0.0},
    ]
    evidence = Evidence(
        deployment={
            "name": "citizen-service",
            "containers": [
                {"name": "citizen-service", "image": PLACEHOLDER_IMAGE, "env": [], "command": [], "args": []}
            ],
        },
        replicaset_history=replicasets,
        health_status="down",
        up=0.0,
    )
    hypothesis = Hypothesis(
        root_cause=RootCause.BAD_DEPLOYMENT,
        confidence=0.9,
        reasoning="the current ReplicaSet's image is a placeholder that was never substituted",
        recommended_action=RemediationAction.ROLLBACK_DEPLOYMENT,
    )
    k8s = FakeKubernetes(available=True, replicasets=replicasets)
    tool_ctx = ToolContext(incident=incident, k8s=k8s)
    return incident, evidence, hypothesis, tool_ctx


@pytest.mark.asyncio
async def test_tool_discovered_image_produces_a_grounded_proposal():
    incident, evidence, hypothesis, tool_ctx = _bad_deployment_incident_and_evidence()
    reasoner = ScriptedReasoner(
        [
            {
                "action": "request_evidence",
                "hypothesis": "the current image may be a bad/placeholder deploy; check history",
                "evidence_source": "inspect_previous_revision",
                "parameters": {},
            },
            {
                "action": "propose",
                "hypothesis": "revision 2 ran a real, valid image before the bad deploy",
                "problem": "citizen-service is running a placeholder image that was never substituted.",
                "root_cause": "a bad deployment left an unsubstituted CI/CD image placeholder in place",
                "action_type": "update_container_image",
                "container": "citizen-service",
                "image": REAL_IMAGE,
                "reason": "revision 2's ReplicaSet, the newest valid older revision, ran this exact image.",
                "expected_effect": "pods start successfully instead of Init:InvalidImageName.",
                "validation_plan": "confirm pods reach Ready and /readyz responds ok.",
                "confidence": 0.99,
            },
        ]
    )

    proposal, trace = await investigate_deep(
        incident,
        evidence,
        hypothesis,
        attempted_summary=[],
        reasoner=reasoner,
        tool_ctx=tool_ctx,
        trigger=DeepInvestigationTrigger.SUGGEST_FIX,
    )

    assert proposal is not None
    assert proposal.action_type is NovelActionType.UPDATE_CONTAINER_IMAGE
    assert proposal.target.image == REAL_IMAGE
    assert proposal.target.previous_image == PLACEHOLDER_IMAGE
    assert proposal.target.container == "citizen-service"

    # The trace is the audit trail a human/GUI sees: the tool call really
    # happened and really returned the real candidate, not a re-derived
    # summary invented after the fact.
    assert trace.outcome == "proposal"
    assert trace.tool_call_count == 1
    tool_call = trace.iterations[0].tool_call
    assert tool_call is not None
    assert tool_call.tool == "inspect_previous_revision"
    assert tool_call.succeeded is True
    assert "revision" in tool_call.result_summary or "2" in tool_call.result_summary
    assert len(reasoner.user_prompts) == 2
    assert "EVIDENCE RESULT" in reasoner.user_prompts[1]
    assert REAL_IMAGE in reasoner.user_prompts[1]


@pytest.mark.asyncio
async def test_native_style_tool_call_is_rejected_without_running_a_collector():
    incident, evidence, hypothesis, tool_ctx = _bad_deployment_incident_and_evidence()

    class NativeStyleReasoner(ScriptedReasoner):
        pass

    reasoner = NativeStyleReasoner(
        [
            {
                "action": "call_tool",
                "tool": "inspect_replicasets",
                "tool_params": {},
            },
            {"action": "no_safe_fix", "reason": "native-style output was rejected"},
        ]
    )

    proposal, trace = await investigate_deep(
        incident,
        evidence,
        hypothesis,
        attempted_summary=[],
        reasoner=reasoner,
        tool_ctx=tool_ctx,
        trigger=DeepInvestigationTrigger.SUGGEST_FIX,
    )

    assert proposal is None
    assert trace.outcome == "no_safe_fix"
    assert trace.tool_call_count == 0
    assert all(iteration.tool_call is None for iteration in trace.iterations)
    assert "unsupported recipient/envelope format" in reasoner.user_prompts[1]


@pytest.mark.asyncio
async def test_gpt_oss_developer_recipient_output_is_rejected_without_dispatch():
    incident, evidence, hypothesis, tool_ctx = _bad_deployment_incident_and_evidence()
    reasoner = ScriptedReasoner(
        [
            {"name": "developer", "arguments": {"evidence_source": "inspect_replicasets"}},
            {"action": "no_safe_fix"},
        ]
    )

    proposal, trace = await investigate_deep(
        incident,
        evidence,
        hypothesis,
        attempted_summary=[],
        reasoner=reasoner,
        tool_ctx=tool_ctx,
        trigger=DeepInvestigationTrigger.SUGGEST_FIX,
    )

    assert proposal is None
    assert trace.outcome == "no_safe_fix"
    assert trace.tool_call_count == 0
    assert all(iteration.tool_call is None for iteration in trace.iterations)


@pytest.mark.asyncio
async def test_an_image_never_seen_in_evidence_is_refused_even_if_well_formed():
    """A plausible-looking, syntactically valid image the Deployment has
    NEVER actually run — never surfaced by any tool call, never in
    replicaset history, not the current image — must be refused. This is
    the load-bearing guarantee behind `_build_image_target`'s evidence
    grounding: proposal quality comes from what Sentinel actually observed,
    never from trusting the model's say-so, however confident or well
    reasoned it sounds."""
    incident, evidence, hypothesis, tool_ctx = _bad_deployment_incident_and_evidence()
    reasoner = ScriptedReasoner(
        [
            {
                "action": "propose",
                "hypothesis": "just guessing a plausible-looking known-good tag",
                "problem": "citizen-service is running a placeholder image.",
                "root_cause": "bad deployment",
                "action_type": "update_container_image",
                "container": "citizen-service",
                "image": HALLUCINATED_IMAGE,
                "reason": "this looks like a real image reference.",
                "expected_effect": "pods start successfully.",
                "validation_plan": "confirm pods reach Ready.",
                "confidence": 0.99,
            },
        ]
        * 3  # repeats past the consecutive-malformed-turn cap
    )

    proposal, trace = await investigate_deep(
        incident,
        evidence,
        hypothesis,
        attempted_summary=[],
        reasoner=reasoner,
        tool_ctx=tool_ctx,
        trigger=DeepInvestigationTrigger.SUGGEST_FIX,
        max_consecutive_malformed_turns=2,
    )

    assert proposal is None
    assert trace.outcome == "no_safe_fix"
    # It never silently made something up instead — the whole point of
    # generalising from evidence is that "nothing safe to propose" is itself
    # an honest, expected outcome, not a failure mode to paper over.
    assert trace.proposal_id is None
