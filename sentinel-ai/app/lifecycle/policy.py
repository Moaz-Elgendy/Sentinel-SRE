"""
POLICY CHECK — the Policy Engine.

    LLM -> Decision Engine -> **Policy Engine** -> Remediation Engine -> K8s

This is the security-critical module. Everything upstream of it is advisory:
the LLM writes prose, the rule engine forms an opinion, the Decision Engine
proposes an ordering. Nothing upstream can cause a cluster mutation. This
module is what turns a proposal into an authorisation, and the Remediation
Engine refuses to act without one.

Design principles:

* **Deny by default.** `evaluate()` has no fall-through "allow" path; an
  action reaches the API server only by matching an explicit branch that
  passes every check for that action type.
* **Pure and synchronous.** No I/O, no cluster calls, no ambient clock (the
  clock is a parameter). Every input is an argument. That is what makes
  test_policy.py able to exercise the real production code path with no
  network and no Kubernetes, which is the only way this file's guarantees are
  worth anything.
* **The frozen deny-list beats the allow-list.** `ALLOWED_DEPLOYMENTS` is
  env-configurable, which means it is a typo away from including
  `citizen-postgres`. The frozen deny-list is checked first and is not
  configurable, so a bad env value cannot make the databases remediable.

### Databases are not remediable, on purpose

`citizen-postgres` and `notification-postgres` are stateful. Restarting them
drops every connection; rolling them back changes the image under a live data
directory; scaling them is meaningless at best and a split-brain at worst. No
confidence score justifies any of that, so a Postgres incident always
escalates to a human. Same for anything in `kube-system`.

### Rollback is autonomous — there is no human approval gate

By design. If the evidence says a deployment broke production, waiting for a
human to click approve is the outage. So rollback carries no approval step;
instead it carries the *strictest preconditions in the file*: the highest
confidence threshold (0.95), plus every one of the seven checks in
`_check_rollback`. If any single one fails, the rollback is denied and the
orchestrator moves to the next candidate action or escalates. "Autonomous"
here means "no human in the loop", not "fewer checks" — it means the checks
had to be moved from a human's judgement into code, which is what this file
is.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.core.metrics import sentinel_policy_denials_total
from app.models.incident import (
    ActionParams,
    ActionPlan,
    DenialReason,
    Incident,
    PolicyVerdict,
    RemediationAction,
)

logger = logging.getLogger(__name__)


@dataclass
class PolicyConfig:
    """Everything the Policy Engine is allowed to consult, as plain data.

    Built from `settings` via `from_settings()` in production. Constructed
    directly in tests. The engine never reads the global `settings` object,
    so a test cannot accidentally pick up production thresholds and a
    production run cannot be perturbed by a test's monkeypatch.
    """

    allowed_namespaces: frozenset[str]
    allowed_deployments: frozenset[str]
    denied_deployments: frozenset[str]
    denied_namespaces: frozenset[str]
    confidence_rollback: float = 0.95
    confidence_restart: float = 0.90
    confidence_scale: float = 0.90
    confidence_chaos_reset: float = 0.90
    min_replicas: int = 1
    max_replicas: int = 3
    max_actions_per_incident: int = 3
    action_cooldown_seconds: int = 120
    deployment_correlation_window_minutes: int = 30

    @classmethod
    def from_settings(cls, settings_obj: object) -> "PolicyConfig":
        s = settings_obj
        return cls(
            allowed_namespaces=frozenset(s.allowed_namespaces_list),  # type: ignore[attr-defined]
            allowed_deployments=frozenset(s.allowed_deployments_list),  # type: ignore[attr-defined]
            denied_deployments=frozenset(s.denied_deployments_frozen),  # type: ignore[attr-defined]
            denied_namespaces=frozenset(s.denied_namespaces_frozen),  # type: ignore[attr-defined]
            confidence_rollback=s.confidence_threshold_rollback,  # type: ignore[attr-defined]
            confidence_restart=s.confidence_threshold_restart,  # type: ignore[attr-defined]
            confidence_scale=s.confidence_threshold_scale,  # type: ignore[attr-defined]
            confidence_chaos_reset=s.confidence_threshold_chaos_reset,  # type: ignore[attr-defined]
            min_replicas=s.min_replicas,  # type: ignore[attr-defined]
            max_replicas=s.max_replicas,  # type: ignore[attr-defined]
            max_actions_per_incident=s.max_actions_per_incident,  # type: ignore[attr-defined]
            action_cooldown_seconds=s.action_cooldown_seconds,  # type: ignore[attr-defined]
            deployment_correlation_window_minutes=(
                s.deployment_correlation_window_minutes  # type: ignore[attr-defined]
            ),
        )

    def threshold_for(self, action: RemediationAction) -> float:
        """Required confidence for an action.

        Unknown actions get 1.01 — unreachable, i.e. deny. A new enum member
        added without a threshold therefore fails closed rather than
        inheriting a permissive default.
        """
        return {
            RemediationAction.ROLLBACK_DEPLOYMENT: self.confidence_rollback,
            RemediationAction.RESTART_DEPLOYMENT: self.confidence_restart,
            RemediationAction.SCALE_DEPLOYMENT: self.confidence_scale,
            RemediationAction.RESET_CHAOS_FAULT: self.confidence_chaos_reset,
        }.get(action, 1.01)


@dataclass
class PolicyContext:
    """Facts about the world that the Policy Engine needs but cannot fetch.

    Assembled by the orchestrator from the evidence bundle and correlation
    findings. Keeping it as an explicit argument (rather than letting policy
    reach into clients) is what makes the engine pure — and it means every
    precondition a test wants to flip is a single field.

    Defaults are the *conservative* values: no previous revision, no history,
    no correlation, not reversible, no validation available. A caller that
    forgets to populate a field gets a denial, not an authorisation.
    """

    previous_revision_exists: bool = False
    deployment_history_count: int = 0
    last_deploy_age_seconds: float | None = None
    deploy_correlates_with_onset: bool = False
    rollback_reversible: bool = False
    recovery_validation_available: bool = False
    current_replicas: int | None = None
    chaos_surface_available: bool = False
    # Set when the target is known to be a stateful workload. Independent of
    # the name-based deny-list so that a future stateful service (a Redis, a
    # queue) can be excluded without editing the frozen list.
    target_is_stateful: bool = False
    notes: list[str] = field(default_factory=list)


class PolicyEngine:
    def __init__(self, config: PolicyConfig) -> None:
        self.config = config

    # -- public API -------------------------------------------------------
    def evaluate(
        self,
        incident: Incident,
        plan: ActionPlan,
        context: PolicyContext,
        now: float,
    ) -> PolicyVerdict:
        """Authorise or deny one candidate action.

        Order matters: the cheapest and most absolute checks run first so that
        a denial for "you may never touch Postgres" is never masked by a
        denial for "confidence too low", which would be a misleading audit
        trail.
        """
        checks: dict[str, bool] = {}

        # ESCALATE is not a cluster action. It is always permitted because
        # "hand this to a human" must never be blocked by policy.
        if plan.action is RemediationAction.ESCALATE:
            return PolicyVerdict(
                allowed=True,
                action=plan.action,
                detail="escalation is always permitted",
                checks={"escalation": True},
            )

        if plan.action not in (
            RemediationAction.RESTART_DEPLOYMENT,
            RemediationAction.ROLLBACK_DEPLOYMENT,
            RemediationAction.SCALE_DEPLOYMENT,
            RemediationAction.RESET_CHAOS_FAULT,
        ):
            # Fails closed for any enum member added without a policy branch.
            return self._deny(
                plan,
                DenialReason.UNKNOWN_ACTION,
                f"action {plan.action.value} has no policy branch; denied by default",
                checks,
            )

        namespace = plan.params.namespace
        deployment = plan.params.deployment

        # ---- 1. target identity --------------------------------------
        if not namespace or not deployment:
            return self._deny(
                plan,
                DenialReason.MISSING_TARGET,
                "action has no namespace and/or deployment; nothing to act on",
                checks,
            )

        # Frozen deny-lists FIRST. These beat any env configuration.
        if namespace in self.config.denied_namespaces:
            checks["namespace_not_frozen_denied"] = False
            return self._deny(
                plan,
                DenialReason.NAMESPACE_FROZEN_DENY,
                f"namespace {namespace} is on the non-configurable deny-list; "
                "Sentinel will never act on cluster infrastructure",
                checks,
            )
        checks["namespace_not_frozen_denied"] = True

        if deployment in self.config.denied_deployments:
            checks["deployment_not_frozen_denied"] = False
            return self._deny(
                plan,
                DenialReason.DEPLOYMENT_FROZEN_DENY,
                f"{deployment} is on the non-configurable deny-list. It is a "
                "stateful PostgreSQL workload: a restart drops every connection, "
                "a rollback changes the image under a live data directory, and "
                "scaling it is meaningless or dangerous. Database incidents "
                "escalate to a human by design, regardless of confidence.",
                checks,
            )
        checks["deployment_not_frozen_denied"] = True

        if context.target_is_stateful:
            checks["target_not_stateful"] = False
            return self._deny(
                plan,
                DenialReason.STATEFUL_TARGET,
                f"{deployment} is a stateful workload; no autonomous remediation "
                "is permitted against stateful targets",
                checks,
            )
        checks["target_not_stateful"] = True

        # Then the configurable allow-lists.
        if namespace not in self.config.allowed_namespaces:
            checks["namespace_allowed"] = False
            return self._deny(
                plan,
                DenialReason.NAMESPACE_NOT_ALLOWED,
                f"namespace {namespace} is not in ALLOWED_NAMESPACES "
                f"({sorted(self.config.allowed_namespaces)})",
                checks,
            )
        checks["namespace_allowed"] = True

        if deployment not in self.config.allowed_deployments:
            checks["deployment_allowed"] = False
            return self._deny(
                plan,
                DenialReason.DEPLOYMENT_NOT_ALLOWED,
                f"deployment {deployment} is not in ALLOWED_DEPLOYMENTS "
                f"({sorted(self.config.allowed_deployments)})",
                checks,
            )
        checks["deployment_allowed"] = True

        # ---- 2. rate limits -------------------------------------------
        # Checked before confidence so that an incident which has already
        # burned its budget is denied for the honest reason.
        if incident.action_count >= self.config.max_actions_per_incident:
            checks["action_cap"] = False
            return self._deny(
                plan,
                DenialReason.ACTION_CAP_REACHED,
                f"this incident has already executed {incident.action_count} "
                f"actions (cap {self.config.max_actions_per_incident}). Continuing "
                "to act on a service that is not recovering makes things worse; "
                "escalate instead.",
                checks,
            )
        checks["action_cap"] = True

        cooldown_remaining = self._cooldown_remaining(incident, plan, now)
        if cooldown_remaining > 0:
            checks["cooldown"] = False
            return self._deny(
                plan,
                DenialReason.COOLDOWN_ACTIVE,
                f"{plan.action.value} was already applied to "
                f"{namespace}/{deployment} {self.config.action_cooldown_seconds - cooldown_remaining:.0f}s "
                f"ago; {cooldown_remaining:.0f}s of cooldown remain. Repeating an "
                "action before the previous one has settled is how a remediator "
                "turns a blip into a rolling restart loop.",
                checks,
            )
        checks["cooldown"] = True

        # ---- 3. confidence -------------------------------------------
        threshold = self.config.threshold_for(plan.action)
        if plan.confidence < threshold:
            checks["confidence"] = False
            return self._deny(
                plan,
                DenialReason.CONFIDENCE_TOO_LOW,
                f"confidence {plan.confidence:.2f} is below the "
                f"{threshold:.2f} threshold required for {plan.action.value}",
                checks,
            )
        checks["confidence"] = True

        # ---- 4. per-action preconditions ------------------------------
        if plan.action is RemediationAction.ROLLBACK_DEPLOYMENT:
            return self._check_rollback(plan, context, checks)
        if plan.action is RemediationAction.SCALE_DEPLOYMENT:
            return self._check_scale(plan, checks)
        if plan.action is RemediationAction.RESET_CHAOS_FAULT:
            return self._check_chaos_reset(plan, context, checks)

        # restart_deployment: the allow-list, cap, cooldown and confidence
        # checks above are the complete set. A rollout restart is the least
        # destructive write available — it replaces pods with identical pods,
        # the Deployment's rolling-update strategy keeps capacity, and it is
        # trivially repeatable. No extra preconditions are warranted.
        return PolicyVerdict(
            allowed=True,
            action=plan.action,
            detail="restart authorised: target allow-listed, within action cap and "
            "cooldown, confidence above threshold",
            checks=checks,
        )

    # -- per-action preconditions -----------------------------------------
    def _check_rollback(
        self, plan: ActionPlan, context: PolicyContext, checks: dict[str, bool]
    ) -> PolicyVerdict:
        """The seven rollback preconditions. ALL must hold.

        These are not defence-in-depth duplicates of each other; each one
        blocks a distinct way a rollback can be wrong:

        1. previous revision exists   - otherwise there is nothing to roll to
        2. deployment history exists  - otherwise we are guessing at history
        3. deploy correlates w/ onset - otherwise the deploy is not the cause
        4. reversible                 - otherwise we cannot undo our own undo
        5. validation available       - otherwise we cannot tell if it worked
        6. within correlation window  - otherwise "recent" is meaningless
        7. (namespace/deployment allow-list, cap, cooldown, confidence 0.95 —
           already enforced in evaluate())
        """
        if not context.previous_revision_exists:
            checks["previous_revision_exists"] = False
            return self._deny(
                plan,
                DenialReason.NO_PREVIOUS_REVISION,
                "no previous ReplicaSet revision exists, so there is no template "
                "to roll back to",
                checks,
            )
        checks["previous_revision_exists"] = True

        if context.deployment_history_count < 2:
            checks["deployment_history"] = False
            return self._deny(
                plan,
                DenialReason.NO_DEPLOYMENT_HISTORY,
                f"deployment history has {context.deployment_history_count} "
                "revision(s); at least 2 are needed to roll back. If "
                "revisionHistoryLimit pruned them, the history is genuinely gone "
                "and a rollback cannot be reconstructed.",
                checks,
            )
        checks["deployment_history"] = True

        if not context.deploy_correlates_with_onset:
            checks["deploy_correlation"] = False
            return self._deny(
                plan,
                DenialReason.NO_DEPLOY_CORRELATION,
                "no recent deployment correlates with the incident onset. Rolling "
                "back code that did not cause the incident changes what is "
                "running for no reason and destroys the evidence.",
                checks,
            )
        checks["deploy_correlation"] = True

        window_seconds = self.config.deployment_correlation_window_minutes * 60
        age = context.last_deploy_age_seconds
        if age is None or age > window_seconds:
            checks["within_correlation_window"] = False
            return self._deny(
                plan,
                DenialReason.NO_DEPLOY_CORRELATION,
                f"the most recent deployment is "
                f"{'unknown' if age is None else f'{age / 60:.1f} minutes'} old, "
                f"outside the {self.config.deployment_correlation_window_minutes} "
                "minute correlation window",
                checks,
            )
        checks["within_correlation_window"] = True

        if not context.rollback_reversible:
            checks["reversible"] = False
            return self._deny(
                plan,
                DenialReason.NOT_REVERSIBLE,
                "the rollback is not reversible. Sentinel only performs changes it "
                "could undo: a rollback is a roll-forward to an older template, "
                "so the current template must still be recoverable from the "
                "ReplicaSet history afterwards.",
                checks,
            )
        checks["reversible"] = True

        if not context.recovery_validation_available:
            checks["validation_available"] = False
            return self._deny(
                plan,
                DenialReason.VALIDATION_UNAVAILABLE,
                "recovery validation is not available for this service (no health "
                "endpoint mapping and/or no metrics), so Sentinel could not tell "
                "whether the rollback helped. Performing an unverifiable "
                "destructive change is not autonomy, it is guessing.",
                checks,
            )
        checks["validation_available"] = True

        return PolicyVerdict(
            allowed=True,
            action=plan.action,
            detail=(
                "rollback authorised autonomously — no human approval gate by "
                "design. All seven preconditions hold: previous revision exists, "
                "history is present, a deployment correlates with onset inside the "
                f"{self.config.deployment_correlation_window_minutes} minute "
                "window, the change is reversible, recovery validation is "
                f"available, the target is allow-listed, and confidence "
                f"{plan.confidence:.2f} >= {self.config.confidence_rollback:.2f}."
            ),
            checks=checks,
        )

    def _check_scale(
        self, plan: ActionPlan, checks: dict[str, bool]
    ) -> PolicyVerdict:
        """Replica count must be inside [min, max], and min is never 0.

        Note this *clamps* rather than only rejecting: a request for 5 on a
        max of 3 becomes 3, which is a useful action. But a request for 0 is
        rejected outright rather than clamped to 1, because a caller asking
        for 0 is asking for an outage and that is a bug worth surfacing, not
        a number worth rounding.
        """
        requested = plan.params.replicas
        if requested is None:
            checks["replicas_specified"] = False
            return self._deny(
                plan,
                DenialReason.REPLICAS_OUT_OF_BAND,
                "scale action carries no replica count",
                checks,
            )
        checks["replicas_specified"] = True

        if requested < 1:
            checks["replicas_not_zero"] = False
            return self._deny(
                plan,
                DenialReason.REPLICAS_OUT_OF_BAND,
                f"refusing to scale to {requested}. Scaling to zero is an outage, "
                "not a remediation, and Sentinel will never do it.",
                checks,
            )
        checks["replicas_not_zero"] = True

        clamped = max(self.config.min_replicas, min(self.config.max_replicas, requested))
        checks["replicas_in_band"] = True
        adjusted = None
        detail = (
            f"scale to {clamped} authorised "
            f"(band [{self.config.min_replicas}, {self.config.max_replicas}])"
        )
        if clamped != requested:
            adjusted = ActionParams(
                namespace=plan.params.namespace,
                deployment=plan.params.deployment,
                replicas=clamped,
                target_revision=plan.params.target_revision,
                service=plan.params.service,
            )
            detail = (
                f"requested {requested} replicas, clamped to {clamped} by the "
                f"[{self.config.min_replicas}, {self.config.max_replicas}] band"
            )

        return PolicyVerdict(
            allowed=True,
            action=plan.action,
            detail=detail,
            adjusted_params=adjusted,
            checks=checks,
        )

    def _check_chaos_reset(
        self, plan: ActionPlan, context: PolicyContext, checks: dict[str, bool]
    ) -> PolicyVerdict:
        """A chaos reset needs a reachable chaos control plane.

        `chaos_surface_available` is false when CHAOS_ADMIN_TOKEN is unset or
        the target has no known base URL (the frontend has no chaos API).
        Without it the action would always fail, and a guaranteed-failed
        action wastes an attempt from the per-incident cap.
        """
        if not context.chaos_surface_available:
            checks["chaos_surface"] = False
            return self._deny(
                plan,
                DenialReason.NO_CHAOS_SURFACE,
                "no usable chaos control plane for this target: either "
                "CHAOS_ADMIN_TOKEN is unset or the service does not expose "
                "/api/chaos/reset (the frontend does not)",
                checks,
            )
        checks["chaos_surface"] = True

        return PolicyVerdict(
            allowed=True,
            action=plan.action,
            detail=(
                "chaos reset authorised. This is the least destructive action "
                "available: it clears an injected fault flag and touches no "
                "Kubernetes object. Because chaos state is per-pod in-memory, the "
                "Remediation Engine re-verifies via the chaos_* gauges per "
                "kubernetes_pod_name rather than trusting the HTTP 200."
            ),
            checks=checks,
        )

    # -- helpers ----------------------------------------------------------
    def _cooldown_remaining(
        self, incident: Incident, plan: ActionPlan, now: float
    ) -> float:
        """Seconds of cooldown left for this (action, target) pair.

        Only *executed* attempts count. A policy-denied candidate never
        touched anything, so it must not start a cooldown — otherwise one
        denial would lock out the action for two minutes for no reason.
        """
        cooldown = self.config.action_cooldown_seconds
        latest = 0.0
        for attempt in incident.attempts:
            if attempt.result is None:
                continue
            if attempt.plan.target_key != plan.target_key:
                continue
            latest = max(latest, attempt.result.started_at)
        if latest == 0.0:
            return 0.0
        elapsed = now - latest
        return max(0.0, cooldown - elapsed)

    @staticmethod
    def _deny(
        plan: ActionPlan,
        reason: DenialReason,
        detail: str,
        checks: dict[str, bool],
    ) -> PolicyVerdict:
        sentinel_policy_denials_total.labels(
            action=plan.action.value, reason=reason.value
        ).inc()
        logger.warning(
            "policy_denied",
            extra={
                "action": plan.action.value,
                "denial_reason": reason.value,
                "namespace": plan.params.namespace,
                "deployment": plan.params.deployment,
                "confidence": plan.confidence,
            },
        )
        return PolicyVerdict(
            allowed=False,
            action=plan.action,
            reason=reason,
            detail=detail,
            checks=checks,
        )
