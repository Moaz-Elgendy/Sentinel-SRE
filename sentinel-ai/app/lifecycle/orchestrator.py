"""
The orchestrator — drives the whole incident lifecycle.

    DETECTION -> INVESTIGATION -> CORRELATION -> ROOT CAUSE ANALYSIS
    -> REMEDIATION DECISION -> POLICY CHECK -> AUTONOMOUS EXECUTION
    -> RECOVERY VALIDATION -> DOCUMENTATION -> NOTIFICATION -> LEARNING

with a remediation loop: when validation fails we RE-INVESTIGATE, re-run
correlation and RCA on the *new* evidence, pick the next safe action, execute
and validate again. When no safe action remains, ESCALATE.

Re-running RCA on the new evidence (rather than reusing the original
hypothesis) is deliberate. A failed remediation is information: if we
restarted a pod and the errors continued, "bad deployment" becomes more
likely and "transient blip" becomes less likely. Reusing the stale hypothesis
would mean walking down a ladder built from a conclusion the evidence has
since contradicted.

This module owns no logic of its own beyond sequencing and bookkeeping. Every
decision lives in the phase module that owns it. That keeps the security
boundary readable: you can audit policy.py and remediation.py without reading
this file, and nothing here can grant an authorisation.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from app.clients.chaos_client import ChaosClient
from app.clients.github_client import GitHubClient
from app.clients.kubernetes_client import KubernetesClient
from app.clients.loki import LokiClient
from app.clients.prometheus import PrometheusClient
from app.clients.slack_client import SlackClient
from app.core.logging_config import set_incident_id
from app.core.metrics import (
    sentinel_escalations_total,
    sentinel_incidents_total,
    sentinel_open_incidents,
    sentinel_validation_result_total,
)
from app.domain.environment import Environment
from app.lifecycle import correlation, documentation, investigation, learning, rca
from app.lifecycle.evidence_signature import compute_signature, material_changes
from app.lifecycle.decision import DecisionEngine
from app.lifecycle.policy import PolicyConfig, PolicyContext, PolicyEngine
from app.lifecycle.remediation import RemediationEngine, RemediationRefused
from app.lifecycle.validation import RecoveryValidator, ValidationThresholds
from app.models.incident import (
    ActionParams,
    ActionPlan,
    AttemptRecord,
    EscalationReason,
    Evidence,
    Incident,
    IncidentStatus,
    LifecyclePhase,
    RemediationAction,
    ValidationOutcome,
)
from app.store.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

# Hard ceiling on remediate->validate->re-investigate cycles, independent of
# MAX_ACTIONS_PER_INCIDENT. The action cap bounds cluster writes; this bounds
# *work*, including denied candidates. Without it a pathological policy
# configuration that denies everything could spin.
MAX_LIFECYCLE_CYCLES = 5


@dataclass
class SentinelContext:
    """Everything the orchestrator needs, injected.

    Constructed once per registered Environment (main.py, Phase 1: exactly
    one) and reused. Held as a dataclass of interfaces rather than reaching
    for module-level singletons so that a test can substitute a fake
    Prometheus, a fake Kubernetes client and a no-op sleeper and drive the
    entire lifecycle offline.
    """

    settings: Any
    environment: Environment
    store: SQLiteStore
    prom: PrometheusClient
    loki: LokiClient
    k8s: KubernetesClient
    chaos: ChaosClient
    github: GitHubClient
    slack: SlackClient
    reasoner: Any  # app.reasoning.base.Reasoner | None
    policy: PolicyEngine
    remediation: RemediationEngine
    validator: RecoveryValidator
    decision: DecisionEngine
    event_bus: Any = None  # app.core.events.EventBus, optional (see main.py)
    # app.lifecycle.incident_manager.IncidentManager, set by main.py after the
    # orchestrator exists. Optional: the orchestrator works without it (tests,
    # tools) — it is only used to publish activity events.
    incident_manager: Any = None


def build_context(
    settings_obj: Any, store: SQLiteStore, environment: Environment, event_bus: Any = None
) -> SentinelContext:
    """Wire the object graph for ONE environment. The layering is visible
    here on purpose.

    Read the constructor arguments top to bottom and you can see that the
    RemediationEngine gets the Kubernetes client and the allow-lists, the
    PolicyEngine gets only configuration, and neither of them is handed
    anything LLM-shaped. The LLM is reached from exactly one place —
    rca.enrich_with_llm — and it receives evidence, not capabilities.

    Connection info (Kubernetes, Prometheus, Loki, GitHub) comes from
    `environment`, not from `settings_obj` directly — that is the entire
    "external control plane" refactor in one function. `settings_obj` still
    supplies policy thresholds, validation bounds and execution mode
    (dry_run etc), none of which are customer-specific in this phase. If
    Sentinel is ever run against several environments concurrently, this
    function is called once per environment and the resulting contexts are
    kept in a dict keyed by environment id — nothing here assumes it is the
    only one.
    """
    s = settings_obj
    prom = PrometheusClient(
        environment.prometheus.url,
        timeout=environment.prometheus.timeout_seconds,
        bearer_token=environment.prometheus.bearer_token,
    )
    loki = LokiClient(
        environment.loki.url,
        timeout=environment.loki.timeout_seconds,
        bearer_token=environment.loki.bearer_token,
    )
    k8s = KubernetesClient(connection=environment.kubernetes)
    k8s.initialise()
    chaos = ChaosClient(s.chaos_admin_token)

    policy_config = PolicyConfig.from_settings(s)
    policy = PolicyEngine(policy_config)

    remediation_engine = RemediationEngine(
        k8s=k8s,
        chaos=chaos,
        allowed_namespaces=policy_config.allowed_namespaces,
        allowed_deployments=policy_config.allowed_deployments,
        denied_deployments=policy_config.denied_deployments,
        denied_namespaces=policy_config.denied_namespaces,
        min_replicas=s.min_replicas,
        max_replicas=s.max_replicas,
        base_url_resolver=s.base_url_for,
        dry_run=s.dry_run,
    )

    validator = RecoveryValidator(
        prom=prom,
        k8s=k8s,
        thresholds=ValidationThresholds(
            max_error_rate=s.validation_max_error_rate,
            max_p95_seconds=s.validation_max_p95_latency_seconds,
            max_cpu_cores=s.validation_max_cpu_cores,
            max_memory_bytes=s.validation_max_memory_bytes,
            settle_seconds=s.validation_settle_seconds,
            timeout_seconds=s.validation_timeout_seconds,
            poll_interval_seconds=s.validation_poll_interval_seconds,
        ),
        base_url_resolver=s.base_url_for,
    )

    decision = DecisionEngine(min_replicas=s.min_replicas, max_replicas=s.max_replicas)

    from app.reasoning.factory import build_reasoner  # noqa: PLC0415 - avoid import cycle at module load

    return SentinelContext(
        settings=s,
        environment=environment,
        store=store,
        prom=prom,
        loki=loki,
        k8s=k8s,
        chaos=chaos,
        github=GitHubClient(
            environment.github.token or "", environment.github.repository or ""
        ),
        slack=SlackClient(s.slack_webhook_url),
        reasoner=build_reasoner(s),
        policy=policy,
        remediation=remediation_engine,
        validator=validator,
        decision=decision,
        event_bus=event_bus,
    )


class Orchestrator:
    def __init__(self, ctx: SentinelContext) -> None:
        self.ctx = ctx
        # One asyncio.Lock per remediation target ("namespace/deployment").
        # Two incidents may investigate the same Deployment concurrently, but
        # only one at a time may execute + validate an action against it.
        self._target_locks: dict[str, asyncio.Lock] = {}

    # -- concurrency helpers ---------------------------------------------
    def _target_lock(self, plan: ActionPlan) -> asyncio.Lock:
        key = f"{plan.params.namespace}/{plan.params.deployment or plan.params.service}"
        lock = self._target_locks.get(key)
        if lock is None:
            lock = self._target_locks[key] = asyncio.Lock()
        return lock

    def _target_acted_since(
        self, incident: Incident, plan: ActionPlan, evidence: Evidence
    ) -> bool:
        """Did ANOTHER incident execute an action on this app after this
        incident's evidence was collected? (Persisted state, so this also holds
        across a restart.)"""
        acted = self.ctx.store.recent_executed_actions(
            incident.app, since=evidence.collected_at, exclude_incident_id=incident.id
        )
        return bool(acted)

    def _emit(self, incident: Incident, message: str, kind: str) -> None:
        """Real activity line for Sentinel Live (no timeline entry)."""
        manager = getattr(self.ctx, "incident_manager", None)
        if manager is not None:
            manager.emit(incident, message, kind)
            return
        bus = self.ctx.event_bus
        if bus is not None:
            try:
                bus.publish(
                    {
                        "type": "incident_updated",
                        "incident_id": incident.id,
                        "alertname": incident.alertname,
                        "severity": incident.severity.value,
                        "phase": incident.phase.value,
                        "status": incident.status.value,
                        "message": message,
                        "event_kind": kind,
                    }
                )
            except Exception:  # noqa: BLE001 - never affects processing
                pass

    def _save(self, incident: Incident) -> None:
        """Persist WITHOUT publishing the (possibly stale) last timeline line."""
        try:
            self.ctx.store.upsert_incident(incident.to_dict())
        except Exception as exc:  # noqa: BLE001
            logger.error("incident_persist_failed", extra={"error_detail": str(exc)[:200]})

    # -- persistence helper ----------------------------------------------
    def _persist(self, incident: Incident) -> None:
        """Save after every phase.

        Called often and deliberately: if Sentinel's pod is evicted halfway
        through remediating, the record must still show what it had done. An
        audit trail that only exists on success is not an audit trail.
        """
        try:
            self.ctx.store.upsert_incident(incident.to_dict())
            sentinel_open_incidents.set(self.ctx.store.count_open())
        except Exception as exc:  # noqa: BLE001
            logger.error("incident_persist_failed", extra={"error_detail": str(exc)[:200]})
            return

        # GUI real-time hook (Phase B of the approved Sentinel GUI plan).
        # This is a SIGNAL to re-fetch, not the state itself — see
        # app/core/events.py and routers/events.py. It is deliberately the
        # only orchestrator change Phase B makes: one publish call, right
        # after the same persist that was already the lifecycle's real,
        # audited checkpoint, so a dropped/never-opened GUI connection can
        # never cause a different outcome than a connected one.
        if self.ctx.event_bus is not None:
            try:
                # `timeline[-1]` is the message `Incident.record()` just
                # wrote for THIS phase transition — real, human-authored
                # text from the actual lifecycle module that ran (rca.py,
                # remediation.py, etc.), not a label invented here. This is
                # what lets Sentinel Live show authentic "what is Sentinel
                # doing" text without adding any new instrumentation.
                last_entry = incident.timeline[-1] if incident.timeline else None
                self.ctx.event_bus.publish(
                    {
                        "type": "incident_updated",
                        "incident_id": incident.id,
                        "alertname": incident.alertname,
                        "severity": incident.severity.value,
                        "phase": incident.phase.value,
                        "status": incident.status.value,
                        "message": last_entry.message if last_entry else None,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                # A GUI notification problem must never affect incident
                # processing, which has already been safely persisted above.
                logger.error("incident_event_publish_failed", extra={"error_detail": str(exc)[:200]})

    # -- the lifecycle ----------------------------------------------------
    async def run(self, incident: Incident) -> Incident:
        """Drive one incident to a terminal state. Never raises.

        A crash here would leave an incident stuck mid-lifecycle with no
        record and no escalation, which is the worst possible failure mode for
        an SRE agent — silently doing nothing while a human assumes it is
        handling things. So the whole body is wrapped and any unexpected
        exception becomes an escalation.
        """
        set_incident_id(incident.id)
        try:
            return await self._run_inner(incident)
        except Exception as exc:  # noqa: BLE001
            logger.exception("lifecycle_internal_error")
            self._escalate(
                incident,
                EscalationReason.INTERNAL_ERROR,
                f"Sentinel hit an internal error and stopped: "
                f"{type(exc).__name__}: {str(exc)[:300]}. A human must take over.",
            )
            await self._finish(incident)
            return incident
        finally:
            set_incident_id(None)

    async def authorize_and_remediate(
        self,
        incident: Incident,
        action: RemediationAction,
        authorization_id: str,
        requested_params: ActionParams | None = None,
    ) -> Incident:
        """Execute exactly ONE SRE-authorized action against an escalated
        incident, through the same Policy Engine and Remediation Engine as
        every autonomous action — never a second path to the cluster.

        This exists for the Sentinel GUI's temporary SRE authorization
        feature (GUI spec section 7 / approved plan Phase D). Called by
        routers/authorizations.py ONLY after it has confirmed a real,
        unexpired, unconsumed `TemporaryAuthorization` row exists for this
        exact incident and action (see app/store/sqlite_store.py) — this
        method trusts that check happened and does not repeat it, but it
        does NOT trust the authorization to mean "skip safety checks": it
        calls `PolicyEngine.evaluate(..., human_override=True)`, which (see
        that parameter's docstring in lifecycle/policy.py) substitutes a
        human's judgement for the model's confidence *number* and nothing
        else. Every other check — frozen deny-lists, allow-lists, the action
        cap, the cooldown, and the chosen action's own preconditions — runs
        exactly as it would for a fully autonomous candidate, and can still
        deny this request.

        Deliberately bounded to ONE attempt, unlike the autonomous
        `_run_inner` loop: a temporary authorization is a one-time exception
        for one action, not a standing invitation for Sentinel to keep
        trying things on its own initiative with a now-spent grant. Evidence
        and correlation are re-collected fresh (not reused from whenever the
        incident originally escalated) because cluster state may have moved
        on since — a stale "previous revision exists" or "deploy correlates
        with onset" would be exactly the kind of unverifiable guess
        policy.py's rollback preconditions exist to prevent.

        Marks the authorization consumed the moment execution actually
        happens (matching the plan's "single-use" design) — a policy denial
        never touches the cluster, so it does not consume the grant.
        """
        set_incident_id(incident.id)
        try:
            incident.status = IncidentStatus.INVESTIGATING
            evidence = await self._investigate(incident, LifecyclePhase.RE_INVESTIGATION)
            incident.evidence = evidence
            self._persist(incident)

            findings = correlation.correlate(
                incident=incident,
                evidence=evidence,
                # Read from the Policy Engine's own config, not `settings`
                # directly — `PolicyConfig` is the single source of truth
                # for this value (see the Sentinel Administration & Tuning
                # Center work: it is now live-editable via
                # `PUT /api/config/policy`, and `policy.py`'s own rollback
                # precondition check reads this exact same
                # `self.ctx.policy.config.deployment_correlation_window_minutes`
                # a few calls downstream of here). Reading from `settings`
                # instead would silently reintroduce a second, driftable
                # copy of the same value the moment an admin changes it.
                correlation_window_minutes=(
                    self.ctx.policy.config.deployment_correlation_window_minutes
                ),
                # Same reasoning as the correlation window above, and same
                # fix: `ctx.validator.thresholds` (a `ValidationThresholds`
                # instance — see lifecycle/validation.py) is what recovery
                # validation actually checks against, and it is now
                # live-editable via `PUT /api/config/rca` (see
                # app/routers/rca_config.py). Reading `ctx.settings.
                # validation_max_*` here instead would silently
                # re-duplicate the same value: correlation would see an
                # admin's change immediately, but recovery validation a
                # few phases later would still be enforcing the old one.
                cpu_threshold_cores=self.ctx.validator.thresholds.max_cpu_cores,
                error_rate_threshold=self.ctx.validator.thresholds.max_error_rate,
                p95_threshold_seconds=self.ctx.validator.thresholds.max_p95_seconds,
            )
            incident.record(
                LifecyclePhase.CORRELATION,
                f"re-correlated evidence for the authorized {action.value} into "
                f"{len(evidence.correlations)} finding(s)",
                findings=findings.to_dict(),
            )
            self._persist(incident)

            params = requested_params or ActionParams(
                namespace=incident.namespace,
                deployment=incident.app,
                service=incident.app,
            )
            confidence = incident.hypothesis.confidence if incident.hypothesis else 0.0
            plan = ActionPlan(
                action=action,
                params=params,
                confidence=confidence,
                rationale=(
                    f"temporary SRE authorization {authorization_id}: an SRE administrator "
                    f"granted a one-time exception for this incident and action only. "
                    f"Sentinel's own confidence remains {confidence:.2f}; the permanent "
                    "policy thresholds are unchanged."
                ),
            )
            context = self._policy_context(incident, findings)
            verdict = self.ctx.policy.evaluate(
                incident, plan, context, now=time.time(), human_override=True
            )
            incident.record(
                LifecyclePhase.POLICY_CHECK,
                f"[temporary authorization {authorization_id}] {action.value}: "
                + ("ALLOWED" if verdict.allowed else "DENIED")
                + f" — {verdict.detail}",
                action=action.value,
                allowed=verdict.allowed,
                denial_reason=verdict.reason.value if verdict.reason else None,
                checks=verdict.checks,
                authorization_id=authorization_id,
            )

            if not verdict.allowed:
                incident.attempts.append(AttemptRecord(plan=plan, verdict=verdict, result=None))
                incident.status = IncidentStatus.ESCALATED
                self._persist(incident)
                # Not consumed: nothing executed against the cluster, so the
                # grant was not spent — see this method's docstring.
                return incident

            attempt = AttemptRecord(plan=plan, verdict=verdict)
            incident.attempts.append(attempt)
            incident.record(
                LifecyclePhase.AUTONOMOUS_EXECUTION,
                f"executing {action.value} under temporary SRE authorization "
                f"{authorization_id}"
                + (" (DRY_RUN)" if self.ctx.remediation.dry_run else ""),  # live value, see ctx.remediation.dry_run
                params=(verdict.adjusted_params or plan.params).to_dict(),
            )
            self._persist(incident)

            try:
                result = await self.ctx.remediation.execute(plan, verdict)
            except RemediationRefused as exc:
                logger.error(
                    "remediation_refused_after_temporary_authorization",
                    extra={
                        "action": action.value,
                        "authorization_id": authorization_id,
                        "error_detail": str(exc)[:200],
                    },
                )
                incident.status = IncidentStatus.ESCALATED
                incident.record(
                    LifecyclePhase.ESCALATION,
                    f"The Remediation Engine refused an action the Policy Engine "
                    f"authorised under temporary SRE authorization {authorization_id}: "
                    f"{exc}. This is an internal inconsistency in Sentinel, not a normal "
                    "denial — it needs investigation before this authorization mechanism "
                    "is trusted again.",
                    reason=EscalationReason.REMEDIATION_ERROR.value,
                )
                self._persist(incident)
                return await self._finish(incident)

            attempt.result = result
            self.ctx.store.consume_temporary_authorization(
                authorization_id,
                consumed_at=time.time(),
                consumed_result="executed_" + ("succeeded" if result.succeeded else "failed"),
            )
            incident.record(
                LifecyclePhase.AUTONOMOUS_EXECUTION,
                f"{action.value} "
                + ("succeeded" if result.succeeded else "FAILED")
                + f": {result.detail}",
                succeeded=result.succeeded,
                dry_run=result.dry_run,
                duration_seconds=result.duration_seconds,
            )
            self._persist(incident)

            if not result.succeeded:
                incident.status = IncidentStatus.ESCALATED
                self._persist(incident)
                return await self._finish(incident)

            incident.status = IncidentStatus.VALIDATING
            incident.record(
                LifecyclePhase.RECOVERY_VALIDATION,
                "waiting for the settle period, then polling until recovery or "
                f"timeout ({self.ctx.validator.thresholds.timeout_seconds}s)",  # live value, see ctx.validator.thresholds
            )
            report = await self.ctx.validator.validate(
                incident,
                verdict.adjusted_params or plan.params,
                baseline_error_rate=evidence.error_rate,
            )
            attempt.validation = report
            sentinel_validation_result_total.labels(result=report.outcome.value).inc()
            incident.record(
                LifecyclePhase.RECOVERY_VALIDATION,
                f"validation {report.outcome.value}: {report.detail}",
                outcome=report.outcome.value,
                failed_checks=report.failed_checks,
                skipped_checks=report.skipped_checks,
                elapsed_seconds=report.elapsed_seconds,
            )
            self._persist(incident)

            if report.outcome in (ValidationOutcome.PASSED, ValidationOutcome.DEGRADED):
                incident.status = IncidentStatus.RESOLVED
                incident.resolved_at = time.time()
            else:
                # Validation failed or timed out. This is a single, bounded
                # attempt (see this method's docstring) — it does not loop
                # into further autonomous tries with an already-consumed
                # grant. The incident goes back to the SRE.
                incident.status = IncidentStatus.ESCALATED

            return await self._finish(incident)
        except Exception as exc:  # noqa: BLE001
            logger.exception("temporary_authorization_internal_error")
            incident.status = IncidentStatus.ESCALATED
            incident.record(
                LifecyclePhase.ESCALATION,
                f"Sentinel hit an internal error while executing temporary SRE "
                f"authorization {authorization_id}: {type(exc).__name__}: "
                f"{str(exc)[:300]}. A human must take over.",
                reason=EscalationReason.INTERNAL_ERROR.value,
            )
            self._persist(incident)
            return incident
        finally:
            set_incident_id(None)

    async def _run_inner(self, incident: Incident) -> Incident:
        sentinel_incidents_total.labels(
            severity=incident.severity.value, root_cause="pending"
        ).inc()

        cycle = 0
        started = time.time()
        incident.lifecycle_started_at = started
        max_seconds = float(getattr(self.ctx.settings, "max_lifecycle_seconds", 1800))
        while cycle < MAX_LIFECYCLE_CYCLES:
            if cycle > 0 and time.time() - started > max_seconds:
                # Cooperative wall-clock bound, checked between cycles and
                # never by cancelling mid-remediation (which could interrupt a
                # Kubernetes write half-way).
                self._escalate(
                    incident,
                    EscalationReason.LIFECYCLE_TIMEOUT,
                    f"This lifecycle has been running for {time.time() - started:.0f}s "
                    f"(limit {max_seconds:.0f}s) without reaching a resolution. "
                    "Sentinel stops rather than keep working on an incident that is "
                    "not converging.",
                )
                break
            cycle += 1
            phase = (
                LifecyclePhase.INVESTIGATION
                if cycle == 1
                else LifecyclePhase.RE_INVESTIGATION
            )

            # ---- INVESTIGATION / RE-INVESTIGATION ----------------------
            incident.status = IncidentStatus.INVESTIGATING
            evidence = await self._investigate(incident, phase)
            incident.evidence = evidence
            self._persist(incident)

            # ---- CORRELATION -------------------------------------------
            findings = correlation.correlate(
                incident=incident,
                evidence=evidence,
                # See the identical comment in authorize_and_remediate above
                # for why this reads PolicyConfig, not settings, directly.
                correlation_window_minutes=(
                    self.ctx.policy.config.deployment_correlation_window_minutes
                ),
                # See the identical comment in authorize_and_remediate above
                # for why this reads `ctx.validator.thresholds`, not
                # `settings`, directly.
                cpu_threshold_cores=self.ctx.validator.thresholds.max_cpu_cores,
                error_rate_threshold=self.ctx.validator.thresholds.max_error_rate,
                p95_threshold_seconds=self.ctx.validator.thresholds.max_p95_seconds,
            )
            incident.record(
                LifecyclePhase.CORRELATION,
                f"correlated evidence into {len(evidence.correlations)} finding(s)",
                findings=findings.to_dict(),
            )
            self._persist(incident)

            # ---- ROOT CAUSE ANALYSIS -----------------------------------
            hypothesis = rca.analyse(incident, evidence, findings)
            hypothesis = await rca.enrich_with_llm(
                incident,
                evidence,
                hypothesis,
                reasoner=self.ctx.reasoner,
            )
            incident.hypothesis = hypothesis
            incident.record(
                LifecyclePhase.ROOT_CAUSE_ANALYSIS,
                f"root cause: {hypothesis.root_cause.value} "
                f"(confidence {hypothesis.confidence:.2f}, source {hypothesis.source})",
                root_cause=hypothesis.root_cause.value,
                confidence=hypothesis.confidence,
                llm_used=hypothesis.llm_used,
                llm_note=hypothesis.llm_note,
            )
            if hypothesis.llm_status in ("call_failed", "reasoner_unavailable"):
                # A provider problem is a CONDITION recorded on this analysis,
                # not an incident and not a lifecycle state: the incident
                # carries on rules-only, exactly as with no LLM configured.
                incident.record(
                    LifecyclePhase.ROOT_CAUSE_ANALYSIS,
                    "REASONER_UNAVAILABLE: the LLM provider could not be used for this "
                    "analysis; continuing with the deterministic rule-based result "
                    "(confidence is NOT raised to compensate). " + hypothesis.llm_note,
                    llm_status=hypothesis.llm_status,
                )
            self._persist(incident)

            # ---- REMEDIATION DECISION ----------------------------------
            incident.status = IncidentStatus.REMEDIATING
            # Passed per call, not assigned onto the shared DecisionEngine:
            # concurrent incidents must not share mutable decision state.
            learning_bias = learning.load_bias(hypothesis.root_cause, self.ctx.store)
            candidates = self.ctx.decision.candidates(
                incident, hypothesis, findings, learning_bias=learning_bias
            )
            incident.record(
                LifecyclePhase.REMEDIATION_DECISION,
                f"{len(candidates)} candidate action(s): "
                + (", ".join(c.action.value for c in candidates) or "none"),
                candidates=[c.to_dict() for c in candidates],
                learning_bias=learning_bias,
            )
            self._persist(incident)

            if not candidates:
                self._escalate(
                    incident,
                    EscalationReason.NO_SAFE_ACTION,
                    "No candidate remediation action is available for root cause "
                    f"'{hypothesis.root_cause.value}'. "
                    + (
                        "The RCA recommended escalation directly."
                        if hypothesis.recommended_action is RemediationAction.ESCALATE
                        else "Either every applicable action has already been tried in "
                        "this incident, or no action in Sentinel's fixed set of four "
                        "could plausibly address this root cause."
                    ),
                )
                break

            # ---- POLICY CHECK -> EXECUTION -> VALIDATION ----------------
            executed_any = False
            resolved = False
            stale_evidence = False
            for plan in candidates:
                lock = self._target_lock(plan)
                if lock.locked():
                    incident.record(
                        LifecyclePhase.AUTONOMOUS_EXECUTION,
                        f"waiting for another incident's remediation of "
                        f"{plan.params.namespace}/{plan.params.deployment or plan.params.service} "
                        "to finish before acting (one remediation per Deployment at a time)",
                    )
                    self._persist(incident)
                async with lock:
                    if self._target_acted_since(incident, plan, evidence):
                        # Another incident acted on this Deployment after OUR
                        # evidence was collected, so that evidence describes a
                        # system that no longer exists. Deciding from it would
                        # be acting on a stale picture: look again instead.
                        incident.record(
                            LifecyclePhase.RE_INVESTIGATION,
                            "another incident remediated this Deployment while this one "
                            "waited; refreshing evidence before deciding",
                        )
                        self._persist(incident)
                        stale_evidence = True
                        break
                    context = self._policy_context(incident, findings)
                    verdict = self.ctx.policy.evaluate(
                        incident, plan, context, now=time.time()
                    )
                    incident.record(
                        LifecyclePhase.POLICY_CHECK,
                        f"{plan.action.value}: "
                        + ("ALLOWED" if verdict.allowed else "DENIED")
                        + f" — {verdict.detail}",
                        action=plan.action.value,
                        allowed=verdict.allowed,
                        denial_reason=verdict.reason.value if verdict.reason else None,
                        checks=verdict.checks,
                    )
                    if not verdict.allowed:
                        # Record the denial as an attempt with no result, so the
                        # incident document shows what was considered and refused.
                        incident.attempts.append(
                            AttemptRecord(plan=plan, verdict=verdict, result=None)
                        )
                        self._persist(incident)
                        continue

                    attempt = AttemptRecord(plan=plan, verdict=verdict)
                    incident.attempts.append(attempt)

                    # ---- AUTONOMOUS EXECUTION ---------------------------
                    incident.record(
                        LifecyclePhase.AUTONOMOUS_EXECUTION,
                        f"executing {plan.action.value}"
                        + (" (DRY_RUN)" if self.ctx.remediation.dry_run else ""),  # live value, see ctx.remediation.dry_run
                        params=(verdict.adjusted_params or plan.params).to_dict(),
                    )
                    try:
                        result = await self.ctx.remediation.execute(plan, verdict)
                    except RemediationRefused as exc:
                        # A refusal after an ALLOW means the two gates disagree,
                        # which is a bug in Sentinel, not an operational failure.
                        # Escalate immediately rather than trying anything else.
                        logger.error(
                            "remediation_refused_after_authorisation",
                            extra={"action": plan.action.value, "error_detail": str(exc)[:200]},
                        )
                        self._escalate(
                            incident,
                            EscalationReason.REMEDIATION_ERROR,
                            f"The Remediation Engine refused an action the Policy Engine "
                            f"authorised: {exc}. This is an internal inconsistency in "
                            "Sentinel and must be investigated before it is trusted again.",
                        )
                        self._persist(incident)
                        return await self._finish(incident)

                    attempt.result = result
                    executed_any = True
                    incident.record(
                        LifecyclePhase.AUTONOMOUS_EXECUTION,
                        f"{plan.action.value} "
                        + ("succeeded" if result.succeeded else "FAILED")
                        + f": {result.detail}",
                        succeeded=result.succeeded,
                        dry_run=result.dry_run,
                        duration_seconds=result.duration_seconds,
                    )
                    self._persist(incident)

                    if not result.succeeded:
                        # Execution itself failed. Do not validate — there is
                        # nothing to validate. Fall through to the next candidate
                        # in this cycle.
                        continue

                    # ---- RECOVERY VALIDATION ----------------------------
                    incident.status = IncidentStatus.VALIDATING
                    incident.record(
                        LifecyclePhase.RECOVERY_VALIDATION,
                        "waiting for the settle period, then polling until recovery or "
                        f"timeout ({self.ctx.validator.thresholds.timeout_seconds}s)",  # live value, see ctx.validator.thresholds
                    )
                    report = await self.ctx.validator.validate(
                        incident,
                        verdict.adjusted_params or plan.params,
                        baseline_error_rate=evidence.error_rate,
                    )
                    attempt.validation = report
                    sentinel_validation_result_total.labels(result=report.outcome.value).inc()
                    incident.record(
                        LifecyclePhase.RECOVERY_VALIDATION,
                        f"validation {report.outcome.value}: {report.detail}",
                        outcome=report.outcome.value,
                        failed_checks=report.failed_checks,
                        skipped_checks=report.skipped_checks,
                        elapsed_seconds=report.elapsed_seconds,
                    )
                    self._persist(incident)

                    if report.outcome is ValidationOutcome.PASSED:
                        incident.status = IncidentStatus.RESOLVED
                        incident.resolved_at = time.time()
                        resolved = True
                        break

                    if report.outcome is ValidationOutcome.DEGRADED:
                        # Partial recovery: the target is healthy, a downstream is
                        # not. We resolve *this* incident because the thing we were
                        # asked to fix is fixed, and we say so loudly rather than
                        # burning more actions on a service that is not the
                        # problem. Chaining remediation into a downstream service
                        # on our own initiative would be Sentinel deciding to widen
                        # its own scope mid-incident.
                        incident.status = IncidentStatus.RESOLVED
                        incident.resolved_at = time.time()
                        incident.record(
                            LifecyclePhase.RECOVERY_VALIDATION,
                            "resolved with a caveat: the target service recovered but "
                            "/readyz still reports status=degraded, so a downstream "
                            "dependency remains unreachable. Sentinel does not "
                            "autonomously remediate a different service than the one "
                            "the alert named; a separate alert would be needed.",
                        )
                        resolved = True
                        break

                    # Validation failed or timed out -> break out of the candidate
                    # loop and go round the lifecycle again with fresh evidence.
                    incident.record(
                        LifecyclePhase.RE_INVESTIGATION,
                        "remediation did not restore service; re-investigating with "
                        "fresh evidence before choosing the next action",
                    )
                    break

            self._persist(incident)
            if resolved:
                break

            if stale_evidence:
                # Bounded: each pass costs one of MAX_LIFECYCLE_CYCLES.
                continue

            if not executed_any:
                # Every candidate in this cycle was denied by policy. Going
                # round again would produce the same denials, because policy is
                # deterministic and the evidence has not changed enough to
                # matter. Escalate now rather than spinning.
                reasons = sorted(
                    {
                        a.verdict.reason.value
                        for a in incident.attempts
                        if a.verdict and a.verdict.reason
                    }
                )
                self._escalate(
                    incident,
                    EscalationReason.NO_SAFE_ACTION,
                    "Every candidate action was denied by the Policy Engine "
                    f"({', '.join(reasons) or 'no reason recorded'}). Sentinel will "
                    "not weaken its own policy to act, so this needs a human. If a "
                    "denial was wrong, adjust the thresholds or allow-lists in "
                    "configuration rather than the checks.",
                )
                break

            if incident.action_count >= self.ctx.settings.max_actions_per_incident:
                self._escalate(
                    incident,
                    EscalationReason.ACTION_CAP_REACHED,
                    f"Sentinel executed {incident.action_count} remediation action(s) "
                    f"(cap {self.ctx.settings.max_actions_per_incident}) and the "
                    "service still did not pass recovery validation. Continuing to "
                    "act on a service that is not recovering makes an incident "
                    "worse, so this stops here.",
                )
                break
        else:
            # while-loop exhausted without break.
            self._escalate(
                incident,
                EscalationReason.VALIDATION_FAILED,
                f"Sentinel completed {MAX_LIFECYCLE_CYCLES} full lifecycle cycles "
                "without the service passing recovery validation.",
            )

        return await self._finish(incident)

    # -- phases split out for readability ---------------------------------
    async def _investigate(
        self, incident: Incident, phase: LifecyclePhase
    ) -> Evidence:
        incident.record(phase, "gathering evidence from Prometheus, Loki and the "
                               "Kubernetes API in parallel")
        self._emit(incident, "Investigation started: gathering Prometheus, Loki and Kubernetes evidence", "investigation_started")
        evidence = await investigation.investigate(
            incident=incident,
            prom=self.ctx.prom,
            loki=self.ctx.loki,
            k8s=self.ctx.k8s,
            health_probe=self._health_probe,
            github=self.ctx.github,
        )
        incident.record(
            phase,
            "evidence collected"
            + (f" with {len(evidence.errors)} collector failure(s)" if evidence.errors else ""),
            error_rate=evidence.error_rate,
            p95_latency_seconds=evidence.p95_latency_seconds,
            up=evidence.up,
            collector_errors=evidence.errors,
        )
        # What each source actually returned — real values, not a script.
        self._emit(
            incident,
            f"Prometheus evidence gathered (error_rate={evidence.error_rate}, "
            f"p95={evidence.p95_latency_seconds}, up={evidence.up})",
            "evidence_prometheus",
        )
        self._emit(
            incident,
            f"Loki queried: {evidence.log_error_count} error log line(s)",
            "evidence_loki",
        )
        self._emit(
            incident,
            f"Kubernetes state inspected: {len(evidence.pods)} pod(s), "
            f"{len(evidence.k8s_events)} event(s), restarts={evidence.restart_count_total}"
            if self.ctx.k8s.available
            else "Kubernetes API unavailable; no cluster evidence",
            "evidence_kubernetes",
        )
        return evidence

    async def _health_probe(self, deployment: str) -> dict[str, Any]:
        """Probe the target's health endpoint, if we know its URL.

        The frontend is probed on /healthz (plain text `ok`); the two Python
        services on /readyz, because /readyz is the one that reveals a
        degraded downstream and a down database in its JSON body.
        """
        from app.lifecycle.validation import probe_health  # noqa: PLC0415

        base_url = self.ctx.settings.base_url_for(deployment)
        if not base_url:
            return {
                "status": None,
                "http_code": None,
                "checks": {},
                "reachable": False,
                "detail": f"no base URL configured for {deployment}",
            }
        path = "/healthz" if deployment == "frontend" else "/readyz"
        return await probe_health(base_url, path=path)

    def _policy_context(
        self, incident: Incident, findings: correlation.CorrelationFindings
    ) -> PolicyContext:
        """Translate evidence + findings into the Policy Engine's inputs.

        Note `rollback_reversible`: a rollback is reversible precisely when the
        *current* template is still recoverable from the ReplicaSet history
        afterwards. Since Kubernetes keeps the current ReplicaSet around when
        we roll back (it becomes just another old revision), having at least
        two revisions means we could roll forward again. That is the whole
        condition — stated here rather than in policy.py because it is a fact
        about the cluster, and policy.py is deliberately free of cluster
        knowledge.
        """
        target = incident.target_deployment
        evidence = incident.evidence
        current_replicas = None
        if evidence and evidence.deployment:
            current_replicas = evidence.deployment.get("desired_replicas")

        chaos_surface = bool(
            self.ctx.chaos.configured
            and target is not None
            # The frontend has no chaos API. Resolving through the settings
            # table rather than a hardcoded name list.
            and self.ctx.settings.base_url_for(target) is not None
            and target != "frontend"
        )

        cooldown = float(self.ctx.policy.config.action_cooldown_seconds)
        other_actions = self.ctx.store.recent_executed_actions(
            incident.app, since=time.time() - cooldown, exclude_incident_id=incident.id
        )

        return PolicyContext(
            other_incident_actions=other_actions,
            previous_revision_exists=findings.previous_revision is not None,
            deployment_history_count=findings.revision_count,
            last_deploy_age_seconds=findings.recent_deployment_age_seconds,
            deploy_correlates_with_onset=findings.deploy_correlates_with_onset,
            rollback_reversible=findings.revision_count >= 2,
            recovery_validation_available=self.ctx.validator.is_available_for(target),
            current_replicas=current_replicas,
            chaos_surface_available=chaos_surface,
            # Name-based for now. The frozen deny-list in policy.py catches the
            # two Postgres Deployments regardless; this flag exists so a future
            # stateful workload can be excluded without editing that list.
            target_is_stateful=bool(target and "postgres" in target.lower()),
        )

    def _escalate(
        self, incident: Incident, reason: EscalationReason, detail: str
    ) -> None:
        """Enter ESCALATED — a controlled terminal state, not "try again".

        Records everything a human (or a later reconsideration) needs: why,
        the RCA and its confidence, every action policy rejected with the
        reason and the confidence it needed, and a coarse signature of the
        evidence used. Repeat alerts are compared against that signature;
        identical evidence never re-runs anything (see
        lifecycle/incident_manager.py and evidence_signature.py).
        """
        now = time.time()
        incident.escalated = True
        incident.escalation_reason = reason
        incident.escalation_detail = detail
        incident.status = IncidentStatus.ESCALATED

        hypothesis = incident.hypothesis
        rejected: list[dict[str, Any]] = []
        since = incident.lifecycle_started_at or 0.0
        for attempt in incident.attempts:
            if attempt.at < since or attempt.result is not None:
                continue
            verdict = attempt.verdict
            if verdict is None or verdict.allowed:
                continue
            try:
                required = self.ctx.policy.config.threshold_for(attempt.plan.action)
            except Exception:  # noqa: BLE001
                required = None
            rejected.append(
                {
                    "action": attempt.plan.action.value,
                    "confidence": attempt.plan.confidence,
                    "required_confidence": required,
                    "denial_reason": verdict.reason.value if verdict.reason else None,
                    "policy_detail": verdict.detail,
                }
            )
        signature = compute_signature(incident.evidence)
        evidence = incident.evidence
        if incident.escalation_record:
            # A previous escalation (before a reopen) is history, never lost.
            pass
        incident.escalation_record = {
            "incident_id": incident.id,
            "at": now,
            "at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "reason": reason.value,
            "detail": detail,
            "root_cause": hypothesis.root_cause.value if hypothesis else None,
            "confidence": hypothesis.confidence if hypothesis else None,
            "recommended_action": hypothesis.recommended_action.value if hypothesis else None,
            "llm_status": hypothesis.llm_status if hypothesis else None,
            "rejected_actions": rejected,
            "policy_reasons": sorted({r["denial_reason"] for r in rejected if r["denial_reason"]}),
            "evidence_signature": signature,
            "evidence_used": (
                {
                    "collected_at": evidence.collected_at,
                    "error_rate": evidence.error_rate,
                    "p95_latency_seconds": evidence.p95_latency_seconds,
                    "memory_bytes": evidence.memory_bytes,
                    "up": evidence.up,
                    "restart_count_total": evidence.restart_count_total,
                    "health_status": evidence.health_status,
                    "log_error_count": evidence.log_error_count,
                    "collector_errors": evidence.errors,
                }
                if evidence
                else None
            ),
            "reopen_count": incident.reopen_count,
        }
        # The baseline future repeats are compared against.
        incident.evidence_baseline = signature
        incident.record(LifecyclePhase.ESCALATION, detail, reason=reason.value)
        sentinel_escalations_total.labels(reason=reason.value).inc()
        logger.warning(
            "incident_escalated",
            extra={
                "escalation_reason": reason.value,
                "alertname": incident.alertname,
                "root_cause": hypothesis.root_cause.value if hypothesis else None,
                "confidence": hypothesis.confidence if hypothesis else None,
                "policy_reasons": incident.escalation_record["policy_reasons"],
            },
        )

    # -- reconsideration of an ESCALATED incident --------------------------
    async def reconsider(
        self, incident: Incident, forced: bool = False, actor: str | None = None
    ) -> str:
        """Look at an ESCALATED incident again, but only for a reason.

        `forced=False` (automatic, triggered by a repeat alert): collect fresh
        evidence, compare its signature to the one recorded at escalation, and
        reopen ONLY if it changed materially (evidence_signature.py) and the
        reopen budget allows. Identical evidence -> nothing runs: no RCA, no
        LLM call, no policy check, no notification. Returns the outcome:
        "no_change" | "baseline_recorded" | "reopened" | "not_escalated".

        `forced=True` (an admin asked): skip the comparison — a person asking
        Sentinel to look again does not need the evidence to have moved — and
        do not spend the automatic reopen budget. The lifecycle it starts is
        the ordinary one: RCA -> Decision -> Policy -> Remediation. It is not a
        second path to the cluster.

        Never raises.
        """
        set_incident_id(incident.id)
        try:
            if incident.status is not IncidentStatus.ESCALATED:
                return "not_escalated"
            now = time.time()
            reasons: list[str]
            if forced:
                reasons = [f"manual re-investigation requested by {actor or 'an administrator'}"]
                self._emit(incident, reasons[0][0].upper() + reasons[0][1:], "reinvestigation_manual")
            else:
                self._emit(incident, "Re-checking evidence for escalated incident", "reconsider_started")
                evidence = await investigation.investigate(
                    incident=incident,
                    prom=self.ctx.prom,
                    loki=self.ctx.loki,
                    k8s=self.ctx.k8s,
                    health_probe=self._health_probe,
                    github=self.ctx.github,
                )
                signature = compute_signature(evidence)
                incident.last_reconsidered_at = now
                if incident.evidence_baseline is None:
                    # Nothing to compare against: record the baseline and stop.
                    # Guessing "changed" here is how loops start.
                    incident.evidence_baseline = signature
                    self._save(incident)
                    self._emit(incident, "Recorded evidence baseline; incident remains ESCALATED", "reconsider_baseline")
                    return "baseline_recorded"
                reasons = material_changes(incident.evidence_baseline, signature)
                if not reasons:
                    self._save(incident)
                    self._emit(
                        incident,
                        "Evidence unchanged since escalation; incident remains ESCALATED "
                        "(no new investigation, decision or notification)",
                        "reconsider_no_change",
                    )
                    return "no_change"
                incident.reopen_count += 1

            # ---- reopen -------------------------------------------------
            if incident.escalation_record:
                incident.escalation_history.append(incident.escalation_record)
            incident.escalated = False
            incident.escalation_reason = None
            incident.escalation_detail = ""
            incident.status = IncidentStatus.INVESTIGATING
            incident.record(
                LifecyclePhase.RE_INVESTIGATION,
                "incident reopened for re-investigation: " + "; ".join(reasons),
                reasons=reasons,
                forced=forced,
                reopen_count=incident.reopen_count,
            )
            self._persist(incident)
            self._emit(incident, "Incident reopened: " + reasons[0], "reopened")
            await self.run(incident)
            return "reopened"
        except Exception as exc:  # noqa: BLE001
            logger.exception("reconsideration_error")
            if incident.status is not IncidentStatus.ESCALATED:
                self._escalate(
                    incident,
                    EscalationReason.INTERNAL_ERROR,
                    f"Sentinel hit an internal error during reconsideration: "
                    f"{type(exc).__name__}: {str(exc)[:300]}. A human must take over.",
                )
                self._save(incident)
            return "error"
        finally:
            set_incident_id(None)

    # -- terminal phases --------------------------------------------------
    async def _finish(self, incident: Incident) -> Incident:
        """DOCUMENTATION -> NOTIFICATION -> LEARNING.

        Runs for every terminal outcome, including escalations and internal
        errors. An escalated incident needs its document *more* than a
        resolved one — that document is what the human on the other end reads.
        """
        # ---- DOCUMENTATION ---------------------------------------------
        try:
            sections = documentation.build_document(incident)
            markdown = documentation.render_markdown(incident)
            incident.documentation = {**sections, "markdown": markdown}
            incident.record(
                LifecyclePhase.DOCUMENTATION,
                "generated incident document (timeline, RCA, impact, resolution, "
                "prevention). Sentinel never modifies application code or merges "
                "anything; code-level suggestions are proposals only.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("documentation_failed", extra={"error_detail": str(exc)[:200]})
            incident.record(
                LifecyclePhase.DOCUMENTATION,
                f"document generation failed: {str(exc)[:200]}",
            )
            markdown = f"Document generation failed: {str(exc)[:200]}"
        self._persist(incident)

        # ---- NOTIFICATION ----------------------------------------------
        notifications: dict[str, Any] = {}
        try:
            github_result, slack_result = await asyncio.gather(
                self.ctx.github.create_issue(
                    title=documentation.issue_title(incident),
                    body=markdown,
                    labels=["incident", "sentinel", incident.severity.value],
                ),
                self.ctx.slack.post(documentation.slack_summary(incident)),
                return_exceptions=True,
            )
            notifications["github"] = (
                {"created": False, "detail": str(github_result)[:200]}
                if isinstance(github_result, BaseException)
                else github_result
            )
            notifications["slack"] = (
                {"sent": False, "detail": str(slack_result)[:200]}
                if isinstance(slack_result, BaseException)
                else slack_result
            )
        except Exception as exc:  # noqa: BLE001
            notifications["error"] = str(exc)[:200]
        incident.notifications = notifications
        incident.record(
            LifecyclePhase.NOTIFICATION,
            "notification delivery attempted",
            github=notifications.get("github"),
            slack=notifications.get("slack"),
        )
        self._persist(incident)

        # ---- LEARNING ---------------------------------------------------
        rows = learning.record_incident_outcomes(incident, self.ctx.store)
        incident.record(
            LifecyclePhase.LEARNING,
            f"recorded {len(rows)} action outcome(s) for future decision bias. "
            "Learning can only make Sentinel more conservative — the bias "
            "multiplier is capped at 1.0, so it can never raise confidence past "
            "a policy threshold.",
            outcomes=rows,
        )

        # Re-label the incident counter now that the root cause is known. The
        # 'pending' series incremented at the start is intentional: it means
        # the total across root_cause values still counts every incident even
        # if Sentinel dies before RCA.
        if incident.hypothesis:
            sentinel_incidents_total.labels(
                severity=incident.severity.value,
                root_cause=incident.hypothesis.root_cause.value,
            ).inc()

        self._persist(incident)
        logger.info(
            "lifecycle_complete",
            extra={
                "status": incident.status.value,
                "escalated": incident.escalated,
                "actions_executed": incident.action_count,
            },
        )
        return incident
