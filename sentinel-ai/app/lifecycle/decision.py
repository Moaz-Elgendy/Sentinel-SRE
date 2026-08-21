"""
REMEDIATION DECISION — the Decision Engine.

Position in the mandated layering:

    LLM  ->  **Decision Engine**  ->  Policy Engine  ->  Remediation Engine  ->  K8s

The Decision Engine's job is to turn a hypothesis into an **ordered list of
candidate ActionPlans**. It decides *what to try, and in what order*. It does
NOT decide whether an action is permitted — that is the Policy Engine, which
sits between this module and anything that touches the cluster.

Why a list and not a single action: the lifecycle requires that a failed
remediation leads to re-investigation and then "the next safe action". Having
the ordering computed here, from the root cause, means "the next safe action"
is a well-defined thing rather than an improvisation. When the list is
exhausted, the answer is ESCALATE — an autonomous agent that keeps inventing
new things to try to a service that is not recovering is strictly worse than
one that stops and pages a human.

Everything in this module is pure. No I/O, no cluster access, no clock except
what is passed in. That is why the ordering logic is unit-testable in
test_decision.py without a cluster.
"""
from __future__ import annotations

import logging

from app.lifecycle.correlation import CorrelationFindings
from app.models.incident import (
    ActionParams,
    ActionPlan,
    Hypothesis,
    Incident,
    RemediationAction,
    RootCause,
)

logger = logging.getLogger(__name__)

# Candidate ordering per root cause, most-appropriate first.
#
# The fallbacks are chosen so each step is *less* specific and no more
# destructive than the one before, never the reverse. Note in particular that
# `rollback_deployment` appears as a fallback for nothing: a rollback is only
# ever attempted when the evidence positively says "a deployment caused
# this". Falling back to a rollback because a restart did not help would mean
# changing what code is running for a reason unrelated to the code, which is
# how a small incident becomes a large one.
#
# `scale_deployment` similarly never appears as a fallback for a fault-shaped
# root cause — adding replicas to a broken service just multiplies the
# breakage.
ACTION_LADDER: dict[RootCause, tuple[RemediationAction, ...]] = {
    RootCause.CHAOS_DATABASE_FAULT: (
        RemediationAction.RESET_CHAOS_FAULT,
        # If clearing the flag genuinely did not take (per-pod state, wrong
        # replica), a rollout restart replaces the pod and therefore its
        # in-memory chaos state. Blunt, but correct and safe.
        RemediationAction.RESTART_DEPLOYMENT,
    ),
    RootCause.CHAOS_HTTP_FAULT: (
        RemediationAction.RESET_CHAOS_FAULT,
        RemediationAction.RESTART_DEPLOYMENT,
    ),
    RootCause.CHAOS_LATENCY_FAULT: (
        RemediationAction.RESET_CHAOS_FAULT,
        RemediationAction.RESTART_DEPLOYMENT,
    ),
    RootCause.CHAOS_NOTIFICATION_FAULT: (
        RemediationAction.RESET_CHAOS_FAULT,
        RemediationAction.RESTART_DEPLOYMENT,
    ),
    RootCause.BAD_DEPLOYMENT: (
        RemediationAction.ROLLBACK_DEPLOYMENT,
        # If the rollback itself fails to apply, a restart at least re-rolls
        # the pods; the Policy Engine will re-evaluate confidence for it.
        RemediationAction.RESTART_DEPLOYMENT,
    ),
    RootCause.MEMORY_LEAK: (RemediationAction.RESTART_DEPLOYMENT,),
    RootCause.POD_CRASH_LOOP: (RemediationAction.RESTART_DEPLOYMENT,),
    RootCause.CPU_SATURATION: (
        RemediationAction.SCALE_DEPLOYMENT,
        RemediationAction.RESTART_DEPLOYMENT,
    ),
    RootCause.CAPACITY_SHORTFALL: (RemediationAction.SCALE_DEPLOYMENT,),
    RootCause.SERVICE_DOWN: (RemediationAction.RESTART_DEPLOYMENT,),
    RootCause.DATABASE_FAILURE: (),  # never remediable — see policy.py
    RootCause.DOWNSTREAM_DEPENDENCY: (),  # not ours to fix; escalate
    RootCause.UNKNOWN: (),  # no confident cause => no autonomous action
}

# Confidence discount applied to each fallback step. The primary candidate
# keeps the hypothesis confidence; the second gets 0.97x, the third 0.94x,
# and so on. This is not a made-up penalty for its own sake: a fallback is by
# construction less well-evidenced than the primary, and because the Policy
# Engine gates on confidence, the discount means a marginal hypothesis
# naturally runs out of permitted actions instead of grinding through the
# whole ladder.
FALLBACK_DISCOUNT = 0.03


class DecisionEngine:
    def __init__(
        self,
        min_replicas: int,
        max_replicas: int,
        learning_bias: dict[str, float] | None = None,
    ) -> None:
        self.min_replicas = min_replicas
        self.max_replicas = max_replicas
        # {action_name: multiplier}. Supplied by learning.py. Bounded there,
        # not here — see the comment in learning.build_bias().
        self.learning_bias = learning_bias or {}

    def candidates(
        self,
        incident: Incident,
        hypothesis: Hypothesis,
        findings: CorrelationFindings,
    ) -> list[ActionPlan]:
        """Ordered candidate actions for this incident.

        Actions already attempted in this incident are filtered out here so
        the orchestrator's loop cannot retry the same thing forever. The
        Policy Engine ALSO enforces this (cooldown + already-attempted), on
        the principle that the last gate before the cluster should not trust
        the caller.
        """
        target = incident.target_deployment
        if hypothesis.recommended_action is RemediationAction.ESCALATE:
            return []
        if not target:
            # No deployment name means no action has a subject. Escalation is
            # the only honest outcome.
            logger.info(
                "no_action_candidates_without_target",
                extra={"alertname": incident.alertname},
            )
            return []

        ladder = ACTION_LADDER.get(hypothesis.root_cause, ())
        if not ladder:
            return []

        # Keep the RCA's own recommendation first even if the ladder happens
        # to disagree — RCA saw the evidence, the ladder is a static table.
        ordered: list[RemediationAction] = []
        if hypothesis.recommended_action in ladder:
            ordered.append(hypothesis.recommended_action)
        for action in ladder:
            if action not in ordered:
                ordered.append(action)

        attempted = {
            attempt.plan.action
            for attempt in incident.attempts
            if attempt.result is not None
        }

        plans: list[ActionPlan] = []
        for index, action in enumerate(ordered):
            if action in attempted:
                continue
            confidence = hypothesis.confidence - (FALLBACK_DISCOUNT * index)
            confidence *= self.learning_bias.get(action.value, 1.0)
            confidence = max(0.0, min(1.0, confidence))
            params = self._params_for(action, incident, findings)
            if params is None:
                continue
            plans.append(
                ActionPlan(
                    action=action,
                    params=params,
                    confidence=confidence,
                    rationale=self._rationale(action, index, hypothesis),
                )
            )

        logger.info(
            "decision_candidates",
            extra={
                "root_cause": hypothesis.root_cause.value,
                "candidate_actions": [p.action.value for p in plans],
                "already_attempted": [a.value for a in attempted],
            },
        )
        return plans

    def _params_for(
        self,
        action: RemediationAction,
        incident: Incident,
        findings: CorrelationFindings,
    ) -> ActionParams | None:
        """Build typed params. Returns None when the action is impossible.

        Note there is no string formatting or command construction anywhere in
        here — the params are structured fields that the Remediation Engine
        maps onto specific API calls.
        """
        target = incident.target_deployment
        namespace = incident.namespace

        if action is RemediationAction.RESTART_DEPLOYMENT:
            return ActionParams(namespace=namespace, deployment=target)

        if action is RemediationAction.ROLLBACK_DEPLOYMENT:
            if findings.previous_revision is None:
                # Nothing to roll back to. Do not emit an unsatisfiable plan
                # just so the Policy Engine can deny it — leave it out.
                return None
            return ActionParams(
                namespace=namespace,
                deployment=target,
                target_revision=findings.previous_revision,
            )

        if action is RemediationAction.SCALE_DEPLOYMENT:
            desired = self._scale_target(incident)
            if desired is None:
                return None
            return ActionParams(
                namespace=namespace, deployment=target, replicas=desired
            )

        if action is RemediationAction.RESET_CHAOS_FAULT:
            return ActionParams(
                namespace=namespace, deployment=target, service=target
            )

        return None

    def _scale_target(self, incident: Incident) -> int | None:
        """One replica more than current, clamped into the band.

        Scaling by +1 rather than doubling because this is a single-node K3s
        cluster: extra replicas beyond what the node can schedule just sit
        Pending, which looks like a failed remediation while actually being a
        capacity limit. Returning None when already at max means the Decision
        Engine emits no scale plan at all, and the orchestrator moves to the
        next candidate or escalates — which is the truthful outcome, since
        "scale up" is genuinely not available.
        """
        evidence = incident.evidence
        current = None
        if evidence and evidence.deployment:
            current = evidence.deployment.get("desired_replicas")
        if current is None:
            current = self.min_replicas
        target = int(current) + 1
        if target > self.max_replicas:
            logger.info(
                "scale_candidate_suppressed_at_max",
                extra={"current_replicas": current, "max_replicas": self.max_replicas},
            )
            return None
        return max(self.min_replicas, target)

    @staticmethod
    def _rationale(
        action: RemediationAction, index: int, hypothesis: Hypothesis
    ) -> str:
        if index == 0:
            return (
                f"primary candidate for root cause {hypothesis.root_cause.value}"
            )
        return (
            f"fallback #{index} for root cause {hypothesis.root_cause.value}; "
            f"confidence discounted by {FALLBACK_DISCOUNT * index:.2f} because a "
            "fallback is by construction less well-evidenced than the primary"
        )
