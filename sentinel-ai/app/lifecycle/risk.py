"""
RISK / IMPACT ANALYSIS — between the Decision Engine and the Policy Engine.

    LLM -> Decision Engine -> **Risk / Impact Analysis** -> Policy Engine -> Remediation Engine -> K8s

What this module is NOT: it is not a second opinion on whether an action is
allowed (that is still, entirely, policy.py's job), and it is not a numeric
"risk score" invented for its own sake. Every field on `RiskAssessment` is a
direct, deterministic read of a fact this codebase already computes or could
trivially compute — this module's only job is to make those facts explicit,
named, and visible in one place, instead of leaving them as scattered
booleans a reviewer has to reconstruct from policy.py's internals.

Concretely, this module answers the questions section 2 of the brief lists:

  * affected resource / service       - `affected_namespace`, `affected_app`
  * potential blast radius            - `blast_radius_scope` (see below)
  * reversibility                     - `reversible`
  * availability of recovery validation - `validation_available`
  * whether a less-impactful action exists and hasn't been tried  -
    `least_impactful_untried_option` (True means: nothing earlier in this
    incident's candidate ladder, for this decision cycle, was skipped over —
    see decision.py's ACTION_LADDER, which is already ordered from least to
    most destructive. This is almost always True by construction, because
    decision.candidates() always proposes the least-impactful untried action
    first. Making it explicit here means a future change to that ordering
    would show up as a visible risk signal instead of silently changing
    behaviour.)

`level` is a closed three-value classification (low / moderate / high)
computed from a fixed table over the fields above — never a float, never an
LLM opinion, and never something Policy is asked to trust blindly: Policy
enforces its own thresholds exactly as before. `level` exists for the audit
trail and the GUI, so a human reviewing an incident can see "Sentinel judged
this a low-impact, reversible, validated action" without reading source.

### Blast radius scope — informational, not a new policy gate

`blast_radius_scope()` classifies whether a candidate's target matches the
incident's own namespace/deployment. decision.py's `_params_for` always
builds params from `incident.namespace`/`incident.target_deployment`, so
in real operation this is always SINGLE_WORKLOAD — the classification is
surfaced (via `PolicyContext.risk` / `PolicyVerdict.risk`, for the audit
trail and GUI) rather than used as a new `policy.py` deny branch: this
module deliberately does not duplicate or re-gate what policy.py's own
deny-lists, confidence thresholds, and per-action preconditions already
enforce. See test_risk.py for what a BEYOND_INCIDENT_SCOPE classification
looks like when a plan's target is deliberately constructed to differ from
the incident's own.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models.incident import ActionPlan, Incident, RemediationAction

# Actions Sentinel can undo with another one of its own four actions, or
# where "undo" is not a meaningful concept because nothing state-changing
# happened. Rollback is the odd one out: whether it is reversible depends on
# whether Kubernetes still has a newer revision to roll forward to
# afterwards, which is a cluster fact (`rollback_reversible`), not a
# property of the action type itself.
_INHERENTLY_REVERSIBLE_ACTIONS = frozenset(
    {
        RemediationAction.RESTART_DEPLOYMENT,
        RemediationAction.SCALE_DEPLOYMENT,
        RemediationAction.RESET_CHAOS_FAULT,
        RemediationAction.ESCALATE,
    }
)

SINGLE_WORKLOAD = "single_workload"
BEYOND_INCIDENT_SCOPE = "beyond_incident_scope"


def blast_radius_scope(incident: Incident, plan: ActionPlan) -> str:
    """SINGLE_WORKLOAD iff `plan` targets exactly the Deployment/namespace
    this incident is about. Shared verbatim with policy.py's own gate so the
    two can never disagree about what "in scope" means."""
    if (
        plan.params.namespace == incident.namespace
        and plan.params.deployment == incident.target_deployment
    ):
        return SINGLE_WORKLOAD
    return BEYOND_INCIDENT_SCOPE


@dataclass
class RiskAssessment:
    affected_namespace: str | None
    affected_deployment: str | None
    affected_app: str | None
    action: str
    blast_radius_scope: str
    reversible: bool
    validation_available: bool
    least_impactful_untried_option: bool
    level: str  # "low" | "moderate" | "high"
    reasoning: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "affected_namespace": self.affected_namespace,
            "affected_deployment": self.affected_deployment,
            "affected_app": self.affected_app,
            "action": self.action,
            "blast_radius_scope": self.blast_radius_scope,
            "reversible": self.reversible,
            "validation_available": self.validation_available,
            "least_impactful_untried_option": self.least_impactful_untried_option,
            "level": self.level,
            "reasoning": self.reasoning,
        }


def _level(
    *,
    scope: str,
    reversible: bool,
    validation_available: bool,
    action: RemediationAction,
) -> tuple[str, list[str]]:
    """Fixed table, not a formula: every branch is one sentence a reviewer
    can check against the fields that produced it."""
    reasons: list[str] = []
    if scope == BEYOND_INCIDENT_SCOPE:
        reasons.append(
            "the action's target is not the workload this incident concerns — "
            "blast radius is not bounded by the incident, so this is treated as "
            "the highest impact class regardless of the action type"
        )
        return "high", reasons

    if not reversible:
        reasons.append(
            f"{action.value} is not reversible in the current cluster state "
            "(no way to undo it with another one of Sentinel's actions)"
        )
        return "high", reasons
    reasons.append(f"{action.value} is reversible in the current cluster state")

    if not validation_available:
        reasons.append(
            "recovery validation is not available for this target, so Sentinel "
            "cannot confirm the action's effect afterwards"
        )
        return "moderate", reasons
    reasons.append("recovery validation is available and will confirm the effect")

    if action is RemediationAction.ROLLBACK_DEPLOYMENT:
        # Reversible and validated, but still a code-version change — never
        # classified as low, even when every other factor is favourable.
        reasons.append("a rollback changes what code is running, so it is never low-impact")
        return "moderate", reasons

    return "low", reasons


def assess_risk(
    incident: Incident,
    plan: ActionPlan,
    *,
    candidate_index: int,
    rollback_reversible: bool,
    recovery_validation_available: bool,
) -> RiskAssessment:
    """Deterministic risk assessment for one candidate action.

    Called once per candidate, in the same order the Policy Engine will
    evaluate them, so `candidate_index` reflects this decision cycle's
    ladder position (0 = the least-impactful untried option — see the
    module docstring).
    """
    scope = blast_radius_scope(incident, plan)
    reversible = (
        rollback_reversible
        if plan.action is RemediationAction.ROLLBACK_DEPLOYMENT
        else plan.action in _INHERENTLY_REVERSIBLE_ACTIONS
    )
    level, reasoning = _level(
        scope=scope,
        reversible=reversible,
        validation_available=recovery_validation_available,
        action=plan.action,
    )
    if scope == SINGLE_WORKLOAD:
        reasoning.insert(0, "targets exactly the workload this incident concerns")

    return RiskAssessment(
        affected_namespace=plan.params.namespace,
        affected_deployment=plan.params.deployment,
        affected_app=incident.app,
        action=plan.action.value,
        blast_radius_scope=scope,
        reversible=reversible,
        validation_available=recovery_validation_available,
        least_impactful_untried_option=candidate_index == 0,
        level=level,
        reasoning=reasoning,
    )
