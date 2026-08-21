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
from app.lifecycle import correlation, documentation, investigation, learning, rca
from app.lifecycle.decision import DecisionEngine
from app.lifecycle.policy import PolicyConfig, PolicyContext, PolicyEngine
from app.lifecycle.remediation import RemediationEngine, RemediationRefused
from app.lifecycle.validation import RecoveryValidator, ValidationThresholds
from app.models.incident import (
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

    Constructed once at app startup (main.py) and reused. Held as a dataclass
    of interfaces rather than reaching for module-level singletons so that a
    test can substitute a fake Prometheus, a fake Kubernetes client and a
    no-op sleeper and drive the entire lifecycle offline.
    """

    settings: Any
    store: SQLiteStore
    prom: PrometheusClient
    loki: LokiClient
    k8s: KubernetesClient
    chaos: ChaosClient
    github: GitHubClient
    slack: SlackClient
    policy: PolicyEngine
    remediation: RemediationEngine
    validator: RecoveryValidator
    decision: DecisionEngine


def build_context(settings_obj: Any, store: SQLiteStore) -> SentinelContext:
    """Wire the object graph. The layering is visible here on purpose.

    Read the constructor arguments top to bottom and you can see that the
    RemediationEngine gets the Kubernetes client and the allow-lists, the
    PolicyEngine gets only configuration, and neither of them is handed
    anything LLM-shaped. The LLM is reached from exactly one place —
    rca.enrich_with_llm — and it receives evidence, not capabilities.
    """
    s = settings_obj
    prom = PrometheusClient(s.prometheus_url)
    loki = LokiClient(s.loki_url)
    k8s = KubernetesClient()
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

    return SentinelContext(
        settings=s,
        store=store,
        prom=prom,
        loki=loki,
        k8s=k8s,
        chaos=chaos,
        github=GitHubClient(s.github_token, s.github_repository),
        slack=SlackClient(s.slack_webhook_url),
        policy=policy,
        remediation=remediation_engine,
        validator=validator,
        decision=decision,
    )


class Orchestrator:
    def __init__(self, ctx: SentinelContext) -> None:
        self.ctx = ctx

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

    async def _run_inner(self, incident: Incident) -> Incident:
        sentinel_incidents_total.labels(
            severity=incident.severity.value, root_cause="pending"
        ).inc()

        cycle = 0
        while cycle < MAX_LIFECYCLE_CYCLES:
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
                correlation_window_minutes=(
                    self.ctx.settings.deployment_correlation_window_minutes
                ),
                cpu_threshold_cores=self.ctx.settings.validation_max_cpu_cores,
                error_rate_threshold=self.ctx.settings.validation_max_error_rate,
                p95_threshold_seconds=(
                    self.ctx.settings.validation_max_p95_latency_seconds
                ),
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
                api_key=self.ctx.settings.openai_api_key,
                model=self.ctx.settings.openai_model,
                timeout=self.ctx.settings.openai_timeout_seconds,
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
            self._persist(incident)

            # ---- REMEDIATION DECISION ----------------------------------
            incident.status = IncidentStatus.REMEDIATING
            self.ctx.decision.learning_bias = learning.load_bias(
                hypothesis.root_cause, self.ctx.store
            )
            candidates = self.ctx.decision.candidates(incident, hypothesis, findings)
            incident.record(
                LifecyclePhase.REMEDIATION_DECISION,
                f"{len(candidates)} candidate action(s): "
                + (", ".join(c.action.value for c in candidates) or "none"),
                candidates=[c.to_dict() for c in candidates],
                learning_bias=self.ctx.decision.learning_bias,
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
            for plan in candidates:
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
                    + (" (DRY_RUN)" if self.ctx.settings.dry_run else ""),
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
                    f"timeout ({self.ctx.settings.validation_timeout_seconds}s)",
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
        evidence = await investigation.investigate(
            incident=incident,
            prom=self.ctx.prom,
            loki=self.ctx.loki,
            k8s=self.ctx.k8s,
            health_probe=self._health_probe,
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

        return PolicyContext(
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
        incident.escalated = True
        incident.escalation_reason = reason
        incident.escalation_detail = detail
        incident.status = IncidentStatus.ESCALATED
        incident.record(LifecyclePhase.ESCALATION, detail, reason=reason.value)
        sentinel_escalations_total.labels(reason=reason.value).inc()
        logger.warning(
            "incident_escalated",
            extra={"escalation_reason": reason.value, "alertname": incident.alertname},
        )

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
