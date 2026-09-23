"""
Sentinel's domain model.

Everything the lifecycle passes around is defined here. Two rules that the
rest of the codebase depends on:

1. **`RemediationAction` is a closed enum.** It is the single source of truth
   for "what Sentinel is physically capable of doing". The LLM is asked to
   return one of these *names*; anything else is rejected outright in
   `RemediationAction.parse()`. There is no `run_command`, no `exec`, no
   `kubectl` member, and adding one would require a code change plus a
   Remediation Engine handler plus an RBAC verb — three separate gates.

2. **Structured params only.** An action carries an `ActionParams` with typed
   fields (`namespace`, `deployment`, `replicas`, `target_revision`), never a
   string to be interpolated into a command. There is nowhere for an
   injected instruction to land.

Pydantic is used for anything crossing an HTTP boundary (webhook payloads,
API responses); plain dataclasses are used for internal lifecycle state,
which keeps the hot path cheap and the equality semantics obvious in tests.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, raw: str | None) -> "Severity":
        """Alertmanager labels are free text; never crash on a new value."""
        if not raw:
            return cls.UNKNOWN
        try:
            return cls(raw.strip().lower())
        except ValueError:
            # e.g. severity="page" or "sev1" from a rule we have not seen.
            return cls.UNKNOWN


class LifecyclePhase(str, Enum):
    """The mandated incident lifecycle, in order.

    RE_INVESTIGATION is a distinct phase (rather than reusing INVESTIGATION)
    so that a timeline reads honestly: "we tried, it failed, we looked again".
    """

    DETECTION = "detection"
    INVESTIGATION = "investigation"
    CORRELATION = "correlation"
    ROOT_CAUSE_ANALYSIS = "root_cause_analysis"
    REMEDIATION_DECISION = "remediation_decision"
    POLICY_CHECK = "policy_check"
    AUTONOMOUS_EXECUTION = "autonomous_execution"
    RECOVERY_VALIDATION = "recovery_validation"
    RE_INVESTIGATION = "re_investigation"
    DOCUMENTATION = "documentation"
    NOTIFICATION = "notification"
    LEARNING = "learning"
    ESCALATION = "escalation"


class IncidentStatus(str, Enum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    REMEDIATING = "remediating"
    VALIDATING = "validating"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    # An incident Alertmanager told us resolved itself before we finished.
    AUTO_RESOLVED = "auto_resolved"

    @property
    def is_terminal(self) -> bool:
        return self in (
            IncidentStatus.RESOLVED,
            IncidentStatus.ESCALATED,
            IncidentStatus.AUTO_RESOLVED,
        )


class RemediationAction(str, Enum):
    """The complete, closed set of things Sentinel can do to the cluster.

    Four write actions plus ESCALATE (a no-op against the cluster: it writes
    documentation and pings a human). If you are reading this looking for the
    place where arbitrary commands get run: it does not exist.
    """

    RESTART_DEPLOYMENT = "restart_deployment"
    ROLLBACK_DEPLOYMENT = "rollback_deployment"
    SCALE_DEPLOYMENT = "scale_deployment"
    RESET_CHAOS_FAULT = "reset_chaos_fault"
    ESCALATE = "escalate"

    @classmethod
    def parse(cls, raw: Any) -> "RemediationAction | None":
        """Strict parse used on the LLM boundary.

        Returns None (never raises, never guesses) for anything that is not
        an exact enum value. A model that hallucinates "delete_namespace" or
        "restart_deployment; rm -rf /" gets None, and the caller falls back
        to the rule-based recommendation.

        The only leniency permitted is surrounding plain spaces and case —
        e.g. " Restart_Deployment " is accepted, since JSON-formatted model
        output legitimately varies that way. Embedded control/whitespace
        characters (\\n, \\r, \\t) anywhere in the string are NOT stripped
        and cause an immediate rejection instead: a trailing newline is not
        cosmetic noise the way a leading space is, and treating it as
        harmless would make "restart_deployment\\n; rm -rf /"-shaped payloads
        one silent normalisation away from slipping past this gate depending
        on what a future caller does with the parsed value downstream.
        """
        if not isinstance(raw, str):
            return None
        if any(c in raw for c in ("\n", "\r", "\t")):
            return None
        candidate = raw.strip(" ").lower()
        for member in cls:
            if member.value == candidate:
                return member
        return None


class RootCause(str, Enum):
    """Bounded vocabulary for root causes.

    Bounded because it is a Prometheus label (see core/metrics.py) and
    because the Decision Engine maps root cause -> candidate actions with a
    table lookup. The LLM writes the *narrative*; it does not get to invent a
    new root cause category.
    """

    CHAOS_DATABASE_FAULT = "chaos_database_fault"
    CHAOS_HTTP_FAULT = "chaos_http_fault"
    CHAOS_LATENCY_FAULT = "chaos_latency_fault"
    CHAOS_NOTIFICATION_FAULT = "chaos_notification_fault"
    BAD_DEPLOYMENT = "bad_deployment"
    MEMORY_LEAK = "memory_leak"
    CPU_SATURATION = "cpu_saturation"
    POD_CRASH_LOOP = "pod_crash_loop"
    SERVICE_DOWN = "service_down"
    CAPACITY_SHORTFALL = "capacity_shortfall"
    DOWNSTREAM_DEPENDENCY = "downstream_dependency"
    DATABASE_FAILURE = "database_failure"
    UNKNOWN = "unknown"


class DenialReason(str, Enum):
    """Why the Policy Engine said no. Also a Prometheus label."""

    NAMESPACE_NOT_ALLOWED = "namespace_not_allowed"
    NAMESPACE_FROZEN_DENY = "namespace_frozen_deny"
    DEPLOYMENT_NOT_ALLOWED = "deployment_not_allowed"
    DEPLOYMENT_FROZEN_DENY = "deployment_frozen_deny"
    STATEFUL_TARGET = "stateful_target"
    CONFIDENCE_TOO_LOW = "confidence_too_low"
    NO_PREVIOUS_REVISION = "no_previous_revision"
    NO_DEPLOYMENT_HISTORY = "no_deployment_history"
    NO_DEPLOY_CORRELATION = "no_deploy_correlation"
    NOT_REVERSIBLE = "not_reversible"
    VALIDATION_UNAVAILABLE = "validation_unavailable"
    REPLICAS_OUT_OF_BAND = "replicas_out_of_band"
    ACTION_CAP_REACHED = "action_cap_reached"
    COOLDOWN_ACTIVE = "cooldown_active"
    ALREADY_ATTEMPTED = "already_attempted"
    MISSING_TARGET = "missing_target"
    NO_CHAOS_SURFACE = "no_chaos_surface"
    UNKNOWN_ACTION = "unknown_action"
    # Deep Investigation only (lifecycle/deep_investigation.py's
    # apply_llm_response already refuses to even construct a proposal naming
    # such a key; this is policy.py's independent second check on the same
    # rule — see PolicyEngine.evaluate_deep_proposal).
    SENSITIVE_ENV_VAR_KEY = "sensitive_env_var_key"
    # Reserved, not currently raised by any policy branch: lifecycle/risk.py
    # can classify a candidate's blast_radius_scope as "beyond_incident_scope"
    # (its target does not match the incident's own namespace/deployment),
    # but that classification is surfaced informationally (PolicyContext.risk
    # / PolicyVerdict.risk) rather than gated here, because test_policy.py's
    # own test design deliberately constructs plans whose target differs
    # from the incident under test to exercise the namespace/deployment
    # deny-list checks in isolation - a hard gate here would deny those
    # legitimately-scoped checks before they ever ran. Kept as a named,
    # bounded value in case a future caller wants to enforce it explicitly.
    BLAST_RADIUS_EXCEEDS_INCIDENT = "blast_radius_exceeds_incident"


class EscalationReason(str, Enum):
    NO_SAFE_ACTION = "no_safe_action"
    ACTION_CAP_REACHED = "action_cap_reached"
    VALIDATION_FAILED = "validation_failed"
    STATEFUL_TARGET = "stateful_target"
    REMEDIATION_ERROR = "remediation_error"
    LOW_CONFIDENCE = "low_confidence"
    UNKNOWN_ALERT = "unknown_alert"
    INTERNAL_ERROR = "internal_error"
    # Bounded-loop terminators. Every automated retry/re-investigation path
    # ends in one of these (or ACTION_CAP_REACHED / VALIDATION_FAILED above)
    # with an explicit reason — see docs/incident-engine.md.
    LIFECYCLE_TIMEOUT = "lifecycle_timeout"
    RETRY_LIMIT_REACHED = "retry_limit_reached"
    # Sentinel restarted while this incident was mid-lifecycle. Remediation
    # may or may not have been applied; a human must look rather than
    # Sentinel blindly re-running a possibly half-finished action.
    INTERRUPTED = "interrupted_by_restart"


# Escalation reasons for which NEW EVIDENCE could plausibly change the
# outcome, so an escalated incident may be automatically reconsidered when
# the evidence materially changes (lifecycle/incident_manager.py). Every
# other reason means Sentinel exhausted what it is allowed to do, or hit an
# internal fault: those wait for a human (temporary authorization or an
# explicit re-run) and are never reopened automatically. Independently, an
# incident on which Sentinel has already EXECUTED an action is never reopened
# automatically either (see IncidentManager._join_escalated).
AUTO_RECONSIDERABLE_REASONS: frozenset[EscalationReason] = frozenset(
    {
        EscalationReason.NO_SAFE_ACTION,
        EscalationReason.LOW_CONFIDENCE,
        EscalationReason.UNKNOWN_ALERT,
    }
)


class ValidationOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    # Health endpoint returned HTTP 200 with {"status":"degraded"} — the
    # service is serving but a downstream is not. Distinct from FAILED
    # because it is a *partial* recovery, and distinct from `not_ready`.
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


# ---------------------------------------------------------------------------
# Webhook / API schemas (pydantic — these cross the wire)
# ---------------------------------------------------------------------------
class AlertmanagerAlert(BaseModel):
    """One entry from the Alertmanager v4 webhook `alerts` array.

    Everything is optional with a default. Alertmanager payload shape drifts
    between versions and different rules populate different labels; a 422
    from this webhook would make Alertmanager retry forever and we would
    learn nothing. Better to accept a sparse alert and let DETECTION decide
    it is unactionable.
    """

    status: str = "firing"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str | None = None  # noqa: N815 - Alertmanager's own casing
    endsAt: str | None = None  # noqa: N815
    fingerprint: str | None = None
    generatorURL: str | None = None  # noqa: N815


class AlertmanagerWebhook(BaseModel):
    version: str = "4"
    groupKey: str | None = None  # noqa: N815
    status: str = "firing"
    receiver: str | None = None
    externalURL: str | None = None  # noqa: N815
    commonLabels: dict[str, str] = Field(default_factory=dict)  # noqa: N815
    commonAnnotations: dict[str, str] = Field(default_factory=dict)  # noqa: N815
    alerts: list[AlertmanagerAlert] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal lifecycle dataclasses
# ---------------------------------------------------------------------------
@dataclass
class TimelineEvent:
    """One line in the incident timeline. This is the audit trail."""

    phase: LifecyclePhase
    message: str
    at: float = field(default_factory=time.time)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "message": self.message,
            "at": self.at,
            "at_iso": iso(self.at),
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TimelineEvent":
        return cls(
            phase=LifecyclePhase(data["phase"]),
            message=data["message"],
            at=data.get("at", time.time()),
            detail=data.get("detail") or {},
        )


@dataclass
class ActionParams:
    """Typed parameters for a remediation action.

    Structured fields ONLY. No command string, no shell, no free text that
    reaches an exec call. `namespace` and `deployment` are re-checked against
    the allow-list inside the Remediation Engine even though the Policy
    Engine already checked them — defence in depth, because the Remediation
    Engine is the last line before the API server.
    """

    namespace: str | None = None
    deployment: str | None = None
    replicas: int | None = None
    target_revision: int | None = None
    # For reset_chaos_fault: which service's chaos API to hit. Resolved to a
    # URL via settings.base_url_for(), so an unknown value cannot become an
    # arbitrary outbound request target.
    service: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "deployment": self.deployment,
            "replicas": self.replicas,
            "target_revision": self.target_revision,
            "service": self.service,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionParams":
        return cls(
            namespace=data.get("namespace"),
            deployment=data.get("deployment"),
            replicas=data.get("replicas"),
            target_revision=data.get("target_revision"),
            service=data.get("service"),
        )


@dataclass
class ActionPlan:
    """A candidate action: what to do, to what, and how sure we are."""

    action: RemediationAction
    params: ActionParams
    confidence: float
    rationale: str = ""

    @property
    def target_key(self) -> str:
        """Identity used for cooldown and dedup bookkeeping."""
        return f"{self.action.value}:{self.params.namespace}/{self.params.deployment or self.params.service}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "params": self.params.to_dict(),
            "confidence": self.confidence,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionPlan":
        return cls(
            action=RemediationAction(data["action"]),
            params=ActionParams.from_dict(data["params"]),
            confidence=data["confidence"],
            rationale=data.get("rationale", ""),
        )


@dataclass
class Evidence:
    """The evidence bundle produced by INVESTIGATION + CORRELATION.

    Every field is optional/defaulted because Sentinel must keep working when
    Loki is down, or when Prometheus is the thing that broke. `errors` records
    which collectors failed so the RCA narrative can say "we could not see X"
    instead of silently treating missing data as healthy.
    """

    collected_at: float = field(default_factory=time.time)

    # Metrics
    error_rate: float | None = None            # ratio of 5xx over 5m
    error_rate_5xx_count: float | None = None
    p95_latency_seconds: float | None = None
    cpu_cores: float | None = None             # rate(process_cpu_seconds_total)
    memory_bytes: float | None = None
    memory_growth_bytes: float | None = None   # delta over the last 30m
    up: float | None = None                    # up{job="kubernetes-pods"}
    request_rate: float | None = None

    # Chaos gauges, per pod: {pod_name: {"chaos_latency_ms": .., ...}}
    chaos_state: dict[str, dict[str, float]] = field(default_factory=dict)
    chaos_injections: dict[str, float] = field(default_factory=dict)

    # Business metrics (useful for impact assessment)
    notification_deliveries: dict[str, float] = field(default_factory=dict)
    notification_dispatch_failures: float | None = None

    # Logs
    log_lines: list[dict[str, Any]] = field(default_factory=list)
    log_error_count: int = 0
    log_sample_messages: list[str] = field(default_factory=list)
    access_log_line_count: int = 0

    # Logs retrieved from a specifically-identified failing init container
    # (see investigation.py). Each entry is the dict shape
    # KubernetesClient.get_container_logs returns: {pod, container, previous,
    # available, error, lines}. Empty whenever no init container was found
    # failing — most incidents never touch this.
    init_container_logs: list[dict[str, Any]] = field(default_factory=list)

    # Kubernetes
    deployment: dict[str, Any] | None = None
    pods: list[dict[str, Any]] = field(default_factory=list)
    k8s_events: list[dict[str, Any]] = field(default_factory=list)
    replicaset_history: list[dict[str, Any]] = field(default_factory=list)
    restart_count_total: int = 0
    latest_revision_age_seconds: float | None = None

    # GitHub commit correlation for the currently-running image tag (see
    # investigation.py). None whenever GITHUB_TOKEN/GITHUB_REPOSITORY are
    # not configured, the image tag is not a real commit SHA (e.g. a
    # CI smoke-test tag), or the lookup failed — never a hard error, since
    # this is correlation context, not something remediation depends on.
    deploy_commit: dict[str, Any] | None = None

    # Health endpoint (parsed JSON `status`, not just the status code — a
    # downstream outage yields HTTP 200 + {"status":"degraded"})
    health_status: str | None = None
    health_http_code: int | None = None
    health_checks: dict[str, Any] = field(default_factory=dict)

    # Correlation findings, human-readable
    correlations: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "collected_at": self.collected_at,
            "collected_at_iso": iso(self.collected_at),
            "error_rate": self.error_rate,
            "error_rate_5xx_count": self.error_rate_5xx_count,
            "p95_latency_seconds": self.p95_latency_seconds,
            "cpu_cores": self.cpu_cores,
            "memory_bytes": self.memory_bytes,
            "memory_growth_bytes": self.memory_growth_bytes,
            "up": self.up,
            "request_rate": self.request_rate,
            "chaos_state": self.chaos_state,
            "chaos_injections": self.chaos_injections,
            "notification_deliveries": self.notification_deliveries,
            "notification_dispatch_failures": self.notification_dispatch_failures,
            "log_error_count": self.log_error_count,
            "log_sample_messages": self.log_sample_messages[:20],
            "access_log_line_count": self.access_log_line_count,
            "deployment": self.deployment,
            "pods": self.pods,
            "k8s_events": self.k8s_events[:50],
            "replicaset_history": self.replicaset_history,
            "restart_count_total": self.restart_count_total,
            "latest_revision_age_seconds": self.latest_revision_age_seconds,
            "init_container_logs": self.init_container_logs,
            "deploy_commit": self.deploy_commit,
            "health_status": self.health_status,
            "health_http_code": self.health_http_code,
            "health_checks": self.health_checks,
            "correlations": self.correlations,
            "errors": self.errors,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Evidence | None":
        if data is None:
            return None
        return cls(
            collected_at=data.get("collected_at", time.time()),
            error_rate=data.get("error_rate"),
            error_rate_5xx_count=data.get("error_rate_5xx_count"),
            p95_latency_seconds=data.get("p95_latency_seconds"),
            cpu_cores=data.get("cpu_cores"),
            memory_bytes=data.get("memory_bytes"),
            memory_growth_bytes=data.get("memory_growth_bytes"),
            up=data.get("up"),
            request_rate=data.get("request_rate"),
            chaos_state=data.get("chaos_state") or {},
            chaos_injections=data.get("chaos_injections") or {},
            notification_deliveries=data.get("notification_deliveries") or {},
            notification_dispatch_failures=data.get("notification_dispatch_failures"),
            log_error_count=data.get("log_error_count", 0),
            log_sample_messages=data.get("log_sample_messages") or [],
            access_log_line_count=data.get("access_log_line_count", 0),
            deployment=data.get("deployment"),
            pods=data.get("pods") or [],
            k8s_events=data.get("k8s_events") or [],
            replicaset_history=data.get("replicaset_history") or [],
            restart_count_total=data.get("restart_count_total", 0),
            latest_revision_age_seconds=data.get("latest_revision_age_seconds"),
            init_container_logs=data.get("init_container_logs") or [],
            deploy_commit=data.get("deploy_commit"),
            health_status=data.get("health_status"),
            health_http_code=data.get("health_http_code"),
            health_checks=data.get("health_checks") or {},
            correlations=data.get("correlations") or [],
            errors=data.get("errors") or [],
            # log_lines is intentionally never in to_dict()'s output (see
            # its own field comment), so there is nothing to restore it
            # from — it stays at its dataclass default, same as it would
            # for a freshly-collected Evidence that had not populated it.
        )


@dataclass
class Hypothesis:
    """The RCA output. `confidence` drives the Policy Engine gate."""

    root_cause: RootCause
    confidence: float
    reasoning: str
    recommended_action: RemediationAction
    # Provenance matters for auditing: was this the deterministic rule
    # engine, or did an LLM move the number?
    source: str = "rules"          # "rules" | "rules+llm"
    llm_used: bool = False
    llm_note: str = ""
    rule_confidence: float | None = None  # pre-LLM value, for comparison
    supporting: list[str] = field(default_factory=list)
    # What happened with the LLM for THIS analysis: "" (not recorded),
    # "not_configured", "ok", "call_failed", or "reasoner_unavailable" (the
    # provider's circuit is open, so the call was skipped). Anything other
    # than "ok" means the hypothesis is rules-only. It is a fact about the
    # provider, never a lifecycle state — see reasoning/health.py.
    llm_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_cause": self.root_cause.value,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "recommended_action": self.recommended_action.value,
            "source": self.source,
            "llm_used": self.llm_used,
            "llm_note": self.llm_note,
            "rule_confidence": self.rule_confidence,
            "supporting": self.supporting,
            "llm_status": self.llm_status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Hypothesis | None":
        if data is None:
            return None
        return cls(
            root_cause=RootCause(data["root_cause"]),
            confidence=data["confidence"],
            reasoning=data.get("reasoning", ""),
            recommended_action=RemediationAction(data["recommended_action"]),
            source=data.get("source", "rules"),
            llm_used=data.get("llm_used", False),
            llm_note=data.get("llm_note", ""),
            rule_confidence=data.get("rule_confidence"),
            supporting=data.get("supporting") or [],
            llm_status=data.get("llm_status", ""),
        )


@dataclass
class PolicyVerdict:
    allowed: bool
    action: RemediationAction
    reason: DenialReason | None = None
    detail: str = ""
    # Policy may *narrow* a plan (e.g. clamp a scale target into the band).
    adjusted_params: ActionParams | None = None
    checks: dict[str, bool] = field(default_factory=dict)
    # The deterministic risk/impact assessment (lifecycle/risk.py) for this
    # exact candidate, echoed back onto the verdict so the audit trail and
    # GUI can show it next to the allow/deny decision it accompanied. Purely
    # informational: nothing above reads this field to decide anything.
    risk: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "action": self.action.value,
            "reason": self.reason.value if self.reason else None,
            "detail": self.detail,
            "adjusted_params": self.adjusted_params.to_dict() if self.adjusted_params else None,
            "checks": self.checks,
            "risk": self.risk,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PolicyVerdict | None":
        if data is None:
            return None
        return cls(
            allowed=data["allowed"],
            action=RemediationAction(data["action"]),
            reason=DenialReason(data["reason"]) if data.get("reason") else None,
            detail=data.get("detail", ""),
            adjusted_params=(
                ActionParams.from_dict(data["adjusted_params"])
                if data.get("adjusted_params")
                else None
            ),
            checks=data.get("checks") or {},
            risk=data.get("risk"),
        )


@dataclass
class DeepPolicyVerdict:
    """The Policy Engine's verdict on a Deep Investigation proposal —
    deliberately a SEPARATE type from `PolicyVerdict` rather than reusing it
    with a placeholder `action`: `PolicyVerdict.action` is typed
    `RemediationAction` (the closed, rule-vetted set) and nothing here is
    from that set. An `allowed=True` verdict means "eligible to be shown to
    a human for authorization" — it is never itself an authorisation to
    execute. See lifecycle/policy.py's `evaluate_deep_proposal`.
    """

    allowed: bool
    action_type: NovelActionType
    reason: DenialReason | None = None
    detail: str = ""
    checks: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "action_type": self.action_type.value,
            "reason": self.reason.value if self.reason else None,
            "detail": self.detail,
            "checks": self.checks,
        }


@dataclass
class RemediationResult:
    action: RemediationAction
    params: ActionParams
    succeeded: bool
    detail: str = ""
    dry_run: bool = False
    started_at: float = field(default_factory=time.time)
    duration_seconds: float = 0.0
    # Anything we may need to undo or reference later (e.g. the revision we
    # rolled back from, the replica count we changed from).
    before: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "params": self.params.to_dict(),
            "succeeded": self.succeeded,
            "detail": self.detail,
            "dry_run": self.dry_run,
            "started_at": self.started_at,
            "started_at_iso": iso(self.started_at),
            "duration_seconds": self.duration_seconds,
            "before": self.before,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RemediationResult | None":
        if data is None:
            return None
        return cls(
            action=RemediationAction(data["action"]),
            params=ActionParams.from_dict(data["params"]),
            succeeded=data["succeeded"],
            detail=data.get("detail", ""),
            dry_run=data.get("dry_run", False),
            started_at=data.get("started_at", time.time()),
            duration_seconds=data.get("duration_seconds", 0.0),
            before=data.get("before") or {},
        )


@dataclass
class DeepRemediationResult:
    """Mirrors `RemediationResult`, for the same reason `DeepPolicyVerdict`
    mirrors `PolicyVerdict` rather than reusing it: `action_type` is a
    `NovelActionType`, not a `RemediationAction`."""

    action_type: NovelActionType
    target: DeepActionTarget
    succeeded: bool
    detail: str = ""
    dry_run: bool = False
    started_at: float = field(default_factory=time.time)
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type.value,
            "target": self.target.to_dict(),
            "succeeded": self.succeeded,
            "detail": self.detail,
            "dry_run": self.dry_run,
            "started_at": self.started_at,
            "started_at_iso": iso(self.started_at),
            "duration_seconds": self.duration_seconds,
        }


@dataclass
class ValidationReport:
    outcome: ValidationOutcome
    checks: dict[str, Any] = field(default_factory=dict)
    failed_checks: list[str] = field(default_factory=list)
    skipped_checks: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.outcome is ValidationOutcome.PASSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "checks": self.checks,
            "failed_checks": self.failed_checks,
            "skipped_checks": self.skipped_checks,
            "elapsed_seconds": self.elapsed_seconds,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ValidationReport | None":
        if data is None:
            return None
        return cls(
            outcome=ValidationOutcome(data["outcome"]),
            checks=data.get("checks") or {},
            failed_checks=data.get("failed_checks") or [],
            skipped_checks=data.get("skipped_checks") or [],
            elapsed_seconds=data.get("elapsed_seconds", 0.0),
            detail=data.get("detail", ""),
        )


@dataclass
class AttemptRecord:
    """One trip around the remediate -> validate loop."""

    plan: ActionPlan
    verdict: PolicyVerdict | None = None
    result: RemediationResult | None = None
    validation: ValidationReport | None = None
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "verdict": self.verdict.to_dict() if self.verdict else None,
            "result": self.result.to_dict() if self.result else None,
            "validation": self.validation.to_dict() if self.validation else None,
            "at": self.at,
            "at_iso": iso(self.at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AttemptRecord":
        return cls(
            plan=ActionPlan.from_dict(data["plan"]),
            verdict=PolicyVerdict.from_dict(data.get("verdict")),
            result=RemediationResult.from_dict(data.get("result")),
            validation=ValidationReport.from_dict(data.get("validation")),
            at=data.get("at", time.time()),
        )


# ---------------------------------------------------------------------------
# DEEP LLM INVESTIGATION / NOVEL TYPED REMEDIATION
#
#   RCA -> Decision -> Policy -> (known remediation) -> Validate
#                          |
#                          `-> insufficient -> **Deep Investigation** (bounded,
#                              read-only, evidence-only) -> structured proposal
#                              -> deep policy check -> human authorization
#                              (always required - this is never autonomous,
#                              unlike the four known actions) -> typed
#                              execution -> Validate
#
# Everything below follows rca.py's "the LLM writes prose, Python decides"
# boundary, one step further: here the LLM does not even choose among a
# rule-engine-approved set, it PROPOSES a novel action from scratch. That
# means every field of what it returns is untrusted until validated, and the
# blast radius of what it can even propose is closed by construction:
# `NovelActionType` is a small, explicit enum, never a free-form string, and
# `DeepActionTarget` has no field that could carry a shell command, a
# manifest, or an arbitrary API call - only the same narrow
# namespace/deployment/container/key/value shape typed remediation actually
# executes through (see lifecycle/deep_investigation.py and
# clients/kubernetes_client.py's patch_deployment_env_var /
# remove_deployment_env_var). A proposal is never executed directly from the
# model's output: apply_llm_response() (deep_investigation.py) re-validates
# every field against the incident it was generated for, and
# PolicyEngine.evaluate_deep_proposal() (lifecycle/policy.py) gates it with
# the same frozen deny-lists, allow-lists, action cap and cooldown as every
# other remediation - plus a confidence floor and a human authorization
# requirement that the four known actions do not carry.
# ---------------------------------------------------------------------------
class NovelActionType(str, Enum):
    """The closed set of typed operations a Deep Investigation proposal may
    request — the "remediation DSL" (spec section 3). Each member maps to
    exactly one narrowly-scoped KubernetesClient write method, never a
    generic "patch this manifest" escape hatch (see that module's own
    docstring on why no such method exists). Extending this set means adding
    a new KubernetesClient method, a new RemediationEngine dispatch branch
    and a new PolicyEngine precondition together - never on its own.

    Six members, deliberately not the full candidate list a novel-remediation
    brief might suggest (e.g. a generic PATCH_DEPLOYMENT_FIELD, a
    PATCH_SERVICE_FIELD, or a PATCH_CONFIG_REFERENCE that points an env var at
    a ConfigMap/Secret key). Those three are left OUT on purpose, not merely
    "not yet done":

      * PATCH_DEPLOYMENT_FIELD, taken generically ("patch any field"), is
        exactly the arbitrary-patch escape hatch this codebase refuses to
        build (see this module's own header docstring). Every field this
        DSL CAN safely patch already has its own narrow, named member below.
      * PATCH_SERVICE_FIELD touches Service selectors/ports, i.e. traffic
        routing — "networking" in the security boundary's own words (see
        lifecycle/deep_investigation.py's module docstring, "must not: ...
        modify networking/security controls"). It is excluded on that
        boundary, not on engineering difficulty.
      * PATCH_CONFIG_REFERENCE (pointing an env var at a ConfigMap/Secret key
        instead of a literal) is excluded because it is one hop from
        "access secrets" — a `valueFrom.secretKeyRef` looks identical in
        shape to a ConfigMap reference in the patch body, and the same
        boundary that refuses SET_ENV_VAR for a sensitive-looking key name
        would have to be re-derived for a reference type most operators
        cannot tell apart from a secret mount at a glance. Not worth the
        risk for a capability nothing in this codebase's remediation history
        has ever needed.

    Each remaining member below fits the SAME architecture the original two
    did: a narrow KubernetesClient method, independent target/parameter
    validation in deep_investigation.apply_llm_response, a PolicyEngine
    precondition in evaluate_deep_proposal, and a RemediationEngine executor
    in execute_deep. See each one's own comment for what makes it safe.
    """

    SET_ENV_VAR = "set_env_var"
    UNSET_ENV_VAR = "unset_env_var"
    # Change one container's image. Deliberately NOT "any image the model
    # names": apply_llm_response only accepts an image that literally
    # appears in this incident's OWN replicaset_history evidence (i.e. a
    # real image this exact Deployment has actually run before) — a
    # generalisation of "roll back", not a door to running arbitrary
    # attacker- or hallucination-supplied images. See that function's
    # `_validate_image_target` for the check.
    UPDATE_CONTAINER_IMAGE = "update_container_image"
    # Change spec.replicas. Overlaps in mechanism with the existing
    # autonomous SCALE_DEPLOYMENT (same KubernetesClient.scale_deployment
    # write, same [min_replicas, max_replicas] band re-asserted by policy and
    # by the engine) but reachable from Deep Investigation's evidence-driven
    # reasoning rather than only the rule engine's fixed replica math — e.g.
    # "these three pods are all OOMKilled and the fourth is healthy, drop to
    # 1 replica to stop the crash loop while a human looks" is a legitimate
    # deep-investigation conclusion the rule engine's decision.py has no
    # branch for.
    UPDATE_REPLICAS = "update_replicas"
    # Override one container's entrypoint/args. Always classified "high"
    # risk (see deep_investigation._assess_deep_risk) — unlike an env var,
    # this changes what the container literally executes, which is a bigger
    # behavioural change than anything else in this DSL. Still typed and
    # bounded: a list[str] argv, applied via a Kubernetes strategic-merge
    # patch, NEVER a shell string — there is no `sh -c` anywhere in the
    # executor, so a value containing shell metacharacters is just an inert
    # argv element, never something a shell interprets.
    UPDATE_CONTAINER_COMMAND = "update_container_command"
    UPDATE_CONTAINER_ARGS = "update_container_args"

    @classmethod
    def parse(cls, raw: Any) -> "NovelActionType | None":
        """Same strict, tolerant-of-casing parse as RemediationAction.parse -
        deliberately duplicated rather than shared, so a change to one enum's
        parsing can never silently affect the other's trust boundary."""
        if not isinstance(raw, str):
            return None
        if any(ch in raw for ch in ("\n", "\r", "\t")):
            return None
        candidate = raw.strip().lower()
        for member in cls:
            if member.value == candidate:
                return member
        return None


# Action types whose target is an environment variable (container/key/value
# on DeepActionTarget). Used by deep_investigation.py, policy.py and
# remediation.py so "is this an env-var-shaped proposal" is asked the same
# way in all three rather than each re-deriving it.
ENV_VAR_ACTION_TYPES: frozenset[NovelActionType] = frozenset(
    {NovelActionType.SET_ENV_VAR, NovelActionType.UNSET_ENV_VAR}
)


class DeepProposalStatus(str, Enum):
    SUGGESTED = "suggested"
    AUTHORIZED = "authorized"
    EXECUTING = "executing"
    EXECUTED = "executed"
    VALIDATED = "validated"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class DeepActionTarget:
    """The closed, typed parameter shape for a novel remediation action.

    No field here can carry a shell command interpreted by a shell, a file
    path outside a container's declared spec, or an arbitrary Kubernetes
    object reference - only exactly what the six typed KubernetesClient
    methods this DSL dispatches to take (`patch_deployment_env_var` /
    `remove_deployment_env_var` / `patch_deployment_container_image` /
    `scale_deployment` / `patch_deployment_container_command` /
    `patch_deployment_container_args`). Every `previous_*` field is captured
    by Python (from live evidence, never from the model) at proposal-build
    time so every action is always revertible without asking the model to
    remember what it saw.

    One dataclass shared by all six action types, rather than one per type,
    because exactly one of {key/value, image, replicas, command, args} is
    ever populated for a given `action_type` and the alternative (six
    near-identical dataclasses) would just move the "which fields are
    actually meaningful here" question from a docstring to a type registry
    without removing it. `NovelActionType` says which field(s) apply; see
    deep_investigation.apply_llm_response for the per-type construction and
    remediation.py's `execute_deep` for the per-type dispatch.
    """

    namespace: str | None = None
    deployment: str | None = None

    # ---- set_env_var / unset_env_var -------------------------------------
    container: str | None = None
    key: str | None = None
    value: str | None = None
    previous_value: str | None = None
    previous_value_existed: bool = False

    # ---- update_container_image -------------------------------------------
    # `container` above is reused as the target container for this action
    # type too — one container-naming field for every per-container op.
    image: str | None = None
    previous_image: str | None = None

    # ---- update_replicas ---------------------------------------------------
    replicas: int | None = None
    previous_replicas: int | None = None

    # ---- update_container_command / update_container_args -----------------
    # Both are argv-shaped lists, NEVER a shell string — see
    # NovelActionType.UPDATE_CONTAINER_COMMAND's own comment. `None` means
    # "use the container's own image ENTRYPOINT/CMD" (clearing an override),
    # which is itself a legitimate, revertible proposal.
    command: list[str] | None = None
    previous_command: list[str] | None = None
    args: list[str] | None = None
    previous_args: list[str] | None = None
    # Whichever of command/args this proposal did NOT touch had no
    # previous value to capture in the first place; these two flags say
    # whether `previous_command`/`previous_args` were populated FROM the
    # container's real spec (True) or are simply unset because this
    # proposal is the other type of override entirely (False) — the same
    # existed/absent distinction `previous_value_existed` already draws for
    # env vars, generalised to a field that can legitimately be `None` on
    # its own account.
    previous_command_existed: bool = False
    previous_args_existed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "deployment": self.deployment,
            "container": self.container,
            "key": self.key,
            "value": self.value,
            "previous_value": self.previous_value,
            "previous_value_existed": self.previous_value_existed,
            "image": self.image,
            "previous_image": self.previous_image,
            "replicas": self.replicas,
            "previous_replicas": self.previous_replicas,
            "command": self.command,
            "previous_command": self.previous_command,
            "args": self.args,
            "previous_args": self.previous_args,
            "previous_command_existed": self.previous_command_existed,
            "previous_args_existed": self.previous_args_existed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DeepActionTarget":
        data = data or {}
        return cls(
            namespace=data.get("namespace"),
            deployment=data.get("deployment"),
            container=data.get("container"),
            key=data.get("key"),
            value=data.get("value"),
            previous_value=data.get("previous_value"),
            previous_value_existed=bool(data.get("previous_value_existed", False)),
            image=data.get("image"),
            previous_image=data.get("previous_image"),
            replicas=data.get("replicas"),
            previous_replicas=data.get("previous_replicas"),
            command=list(data["command"]) if data.get("command") is not None else None,
            previous_command=(
                list(data["previous_command"])
                if data.get("previous_command") is not None
                else None
            ),
            args=list(data["args"]) if data.get("args") is not None else None,
            previous_args=(
                list(data["previous_args"]) if data.get("previous_args") is not None else None
            ),
            previous_command_existed=bool(data.get("previous_command_existed", False)),
            previous_args_existed=bool(data.get("previous_args_existed", False)),
        )


class DeepInvestigationTrigger(str, Enum):
    """How a Deep Investigation run started. Purely descriptive (drives no
    branching in policy/remediation — both proposal sources are gated
    identically), but it is exactly what spec section 1 means by "share
    implementation, do not duplicate logic": both members enter the SAME
    `deep_investigation.investigate_deep` call and the SAME
    `PolicyEngine.evaluate_deep_proposal` gate; this field only records,
    for audit and for the GUI, which door the operator came in through.
    """

    AUTO = "auto"
    SUGGEST_FIX = "suggest_fix"


@dataclass
class ToolCallRecord:
    """One read-only tool invocation inside a bounded investigation loop.

    Always the record of something that actually ran: `deep_investigation.py`
    appends one of these for EVERY tool call it makes, before folding the
    result into the next turn's prompt, so an audit trail entry always
    corresponds to a real KubernetesClient/PrometheusClient/LokiClient call —
    never to something the model merely claimed it saw (see
    deep_investigation_tools.py's module docstring for how that guarantee is
    enforced structurally, not just by convention).
    """

    tool: str
    params: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)
    succeeded: bool = True
    # A bounded, human/audit-readable summary of what the tool returned —
    # never the full payload (see Settings.deep_investigation_tool_output_max_chars).
    result_summary: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "params": self.params,
            "at": self.at,
            "at_iso": iso(self.at),
            "succeeded": self.succeeded,
            "result_summary": self.result_summary,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolCallRecord":
        return cls(
            tool=data["tool"],
            params=data.get("params") or {},
            at=data.get("at", time.time()),
            succeeded=data.get("succeeded", True),
            result_summary=data.get("result_summary", ""),
            error=data.get("error"),
        )


@dataclass
class InvestigationIteration:
    """One turn of the bounded loop: the model's stated hypothesis at that
    point, and the (at most one) tool call it requested. `tool_call` is None
    on the FINAL iteration, which instead ends in a proposal or `no_safe_fix`
    (recorded on the enclosing `DeepInvestigationTrace`, not here)."""

    iteration: int
    hypothesis: str = ""
    tool_call: ToolCallRecord | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "hypothesis": self.hypothesis,
            "tool_call": self.tool_call.to_dict() if self.tool_call else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InvestigationIteration":
        return cls(
            iteration=data["iteration"],
            hypothesis=data.get("hypothesis", ""),
            tool_call=(
                ToolCallRecord.from_dict(data["tool_call"]) if data.get("tool_call") else None
            ),
        )


@dataclass
class DeepInvestigationTrace:
    """The complete, append-only audit record of ONE bounded investigation
    run — every iteration, every tool call and its real result, and how it
    ended. This is what spec section 2's "every returned result must be
    recorded in incident evidence/audit history" means concretely, and what
    the GUI's investigation-progress view (section 10) reads: nothing about
    "investigating" / "proposing" / "unable to produce a safe fix" is ever
    displayed without one of these backing it.

    Kept on the incident as `Incident.deep_investigation_traces`
    (append-only, mirroring `deep_proposals`/`escalation_history`) rather
    than folded into a `DeepRemediationProposal`, because a run that ends in
    `no_safe_fix` produces no proposal at all — the trace is the only record
    that an investigation happened, and it must exist independently of
    whether one succeeded.
    """

    id: str
    incident_id: str
    trigger: DeepInvestigationTrigger
    started_at: float
    finished_at: float | None = None
    iterations: list[InvestigationIteration] = field(default_factory=list)
    # "running" while in flight, then exactly one of "proposal" / "no_safe_fix"
    # / "error". Never "success"/"failure" — that would conflate "did the
    # INVESTIGATION complete" with "was a fix found", which are different
    # questions (a clean no_safe_fix is a fully successful investigation).
    outcome: str = "running"
    proposal_id: str | None = None
    reason: str = ""
    llm_label: str = ""
    tool_call_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "incident_id": self.incident_id,
            "trigger": self.trigger.value,
            "started_at": self.started_at,
            "started_at_iso": iso(self.started_at),
            "finished_at": self.finished_at,
            "finished_at_iso": iso(self.finished_at) if self.finished_at else None,
            "iterations": [i.to_dict() for i in self.iterations],
            "outcome": self.outcome,
            "proposal_id": self.proposal_id,
            "reason": self.reason,
            "llm_label": self.llm_label,
            "tool_call_count": self.tool_call_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeepInvestigationTrace":
        return cls(
            id=data["id"],
            incident_id=data["incident_id"],
            trigger=DeepInvestigationTrigger(data.get("trigger", "auto")),
            started_at=data.get("started_at", time.time()),
            finished_at=data.get("finished_at"),
            iterations=[
                InvestigationIteration.from_dict(i) for i in data.get("iterations") or []
            ],
            outcome=data.get("outcome", "running"),
            proposal_id=data.get("proposal_id"),
            reason=data.get("reason", ""),
            llm_label=data.get("llm_label", ""),
            tool_call_count=data.get("tool_call_count", 0),
        )


@dataclass
class DeepRemediationProposal:
    """A structured, bounded proposal from the Deep LLM Investigation path.

    Every prose field (`problem`, `reason`, `expected_effect`,
    `validation_plan`) is exactly that - prose, for a human to read. Nothing
    in this dataclass is itself an authorisation: `status` starts at
    SUGGESTED and can only reach AUTHORIZED through a human clicking
    authorize (routers/authorizations.py), exactly like the existing
    temporary-SRE-authorization flow for the four known actions - this
    proposal is never autonomous.
    """

    id: str
    incident_id: str
    created_at: float
    problem: str
    root_cause: str
    action_type: NovelActionType
    target: DeepActionTarget
    reason: str
    expected_effect: str
    risk_level: str  # "low" | "moderate" | "high" - computed by Python (lifecycle/risk.py-style table), never by the model
    risk_reasoning: list[str] = field(default_factory=list)
    reversible: bool = True
    validation_plan: str = ""
    confidence: float = 0.0
    status: DeepProposalStatus = DeepProposalStatus.SUGGESTED
    rendered_command: str = ""  # display-only, generated FROM this struct - never executed
    llm_raw: str = ""  # the raw model response, kept for audit
    llm_label: str = ""  # e.g. "openai:gpt-4o-mini" - which provider produced this
    rejected_reason: str | None = None
    authorization_id: str | None = None
    result_detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "incident_id": self.incident_id,
            "created_at": self.created_at,
            "created_at_iso": iso(self.created_at),
            "problem": self.problem,
            "root_cause": self.root_cause,
            "action_type": self.action_type.value,
            "target": self.target.to_dict(),
            "reason": self.reason,
            "expected_effect": self.expected_effect,
            "risk_level": self.risk_level,
            "risk_reasoning": self.risk_reasoning,
            "reversible": self.reversible,
            "validation_plan": self.validation_plan,
            "confidence": self.confidence,
            "status": self.status.value,
            "rendered_command": self.rendered_command,
            "llm_raw": self.llm_raw[:4000],
            "llm_label": self.llm_label,
            "rejected_reason": self.rejected_reason,
            "authorization_id": self.authorization_id,
            "result_detail": self.result_detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeepRemediationProposal":
        return cls(
            id=data["id"],
            incident_id=data["incident_id"],
            created_at=data.get("created_at", time.time()),
            problem=data.get("problem", ""),
            root_cause=data.get("root_cause", ""),
            action_type=NovelActionType(data["action_type"]),
            target=DeepActionTarget.from_dict(data.get("target")),
            reason=data.get("reason", ""),
            expected_effect=data.get("expected_effect", ""),
            risk_level=data.get("risk_level", "high"),
            risk_reasoning=list(data.get("risk_reasoning") or []),
            reversible=data.get("reversible", True),
            validation_plan=data.get("validation_plan", ""),
            confidence=data.get("confidence", 0.0),
            status=DeepProposalStatus(data.get("status", "suggested")),
            rendered_command=data.get("rendered_command", ""),
            llm_raw=data.get("llm_raw", ""),
            llm_label=data.get("llm_label", ""),
            rejected_reason=data.get("rejected_reason"),
            authorization_id=data.get("authorization_id"),
            result_detail=data.get("result_detail", ""),
        )


@dataclass
class Incident:
    """The aggregate root. Persisted to SQLite after every phase."""

    id: str
    fingerprint: str
    alertname: str
    severity: Severity
    app: str | None = None
    namespace: str = "citizen-portal"
    pod: str | None = None
    summary: str = ""
    description: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)

    # Multi-tenancy identifiers (spec section 6). Stamped onto the incident
    # by routers/alerts.py right after detection.build_incident() creates it
    # — the webhook handler knows which Environment it is running against
    # (Phase 1: the single registered one; see main.py). Optional here, not
    # on the detection constructor itself, because build_incident() has no
    # Environment to reach for and every existing detection test constructs
    # an Incident without one. None means "pre-multi-tenancy incident" or
    # "environment unknown". Note: policy.py's allow-lists are still
    # process-global settings, NOT yet scoped per environment_id — that is a
    # known limitation until a second environment exists to design/test the
    # scoping against (see the integration doc's "Known limitations").
    customer_id: str | None = None
    environment_id: str | None = None
    application_id: str | None = None

    status: IncidentStatus = IncidentStatus.OPEN
    phase: LifecyclePhase = LifecyclePhase.DETECTION
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    resolved_at: float | None = None
    started_at_raw: str | None = None

    # How many distinct alert firings folded into this incident.
    firing_count: int = 1

    # ---- Incident identity & correlation (lifecycle/incident_manager.py) ---
    # `fingerprint` (above) is Sentinel's own STABLE incident identity, see
    # compute_incident_key(). It is NOT Alertmanager's alert fingerprint: that
    # one hashes every label including the pod name, so it changes whenever a
    # pod is replaced — i.e. exactly when Sentinel's own remediation restarts
    # something. The raw Alertmanager fingerprints seen are kept for audit.
    failure_class: str | None = None
    alert_fingerprints: list[str] = field(default_factory=list)
    # Last time ANY firing for this identity was seen (dedup staleness is
    # measured from this, not from updated_at which the lifecycle also bumps).
    last_seen_at: float = field(default_factory=time.time)
    # Repeat firings absorbed without doing any work (no lifecycle, no RCA).
    suppressed_repeats: int = 0
    # Nth occurrence of this identity (a recurrence after RESOLVED is a NEW
    # incident with occurrence+1 and a link back, never a reopen of history).
    occurrence: int = 1
    previous_incident_id: str | None = None

    # ---- Bounded retry bookkeeping ----------------------------------------
    lifecycle_started_at: float | None = None
    # Automatic reopens of an ESCALATED incident on materially new evidence.
    reopen_count: int = 0
    last_reconsidered_at: float | None = None
    # Coarse evidence signature at the last decision point (escalation or
    # reopen). Repeat evidence is compared against THIS — see
    # lifecycle/evidence_signature.py for exactly what counts as "material".
    evidence_baseline: dict[str, Any] | None = None

    evidence: Evidence | None = None
    hypothesis: Hypothesis | None = None
    attempts: list[AttemptRecord] = field(default_factory=list)
    timeline: list[TimelineEvent] = field(default_factory=list)

    # Deep LLM Investigation proposals (see the block above this class).
    # Append-only: a rejected or expired proposal stays in the list as its
    # own audit record rather than being overwritten, the same
    # never-destroy-history choice as `escalation_history`.
    deep_proposals: list[DeepRemediationProposal] = field(default_factory=list)
    # Bounds how many times ONE incident may trigger a Deep Investigation
    # call, independent of MAX_LIFECYCLE_CYCLES: a pathological incident that
    # keeps re-escalating must not keep spending LLM calls on new proposals
    # forever (see orchestrator.py's `_maybe_deep_investigate`). Shared by
    # BOTH the automatic path and an operator's explicit "Suggest Fix" —
    # spec section 1 requires them to share the same underlying
    # implementation and the same bound, not a separate budget each.
    deep_investigation_count: int = 0
    # Append-only audit trace of every bounded investigation run (see
    # DeepInvestigationTrace) — the "investigation progress"/"evidence
    # collected" the GUI reads, independent of whether a run produced a
    # proposal. `deep_investigation_running` is true for exactly the
    # duration of an in-flight run (auto or Suggest Fix); nothing else sets
    # it, and the orchestrator always clears it in a `finally`, so it can
    # never get stuck true after Sentinel itself has moved on.
    deep_investigation_traces: list[DeepInvestigationTrace] = field(default_factory=list)
    deep_investigation_running: bool = False

    escalated: bool = False
    escalation_reason: EscalationReason | None = None
    escalation_detail: str = ""
    # Structured escalation audit (why, RCA, confidence, rejected action,
    # policy reason, evidence used, timestamp, incident id). The CURRENT
    # escalation; earlier ones (before an automatic/manual reopen) are kept in
    # `escalation_history` so a reopened incident never loses its past.
    escalation_record: dict[str, Any] = field(default_factory=dict)
    escalation_history: list[dict[str, Any]] = field(default_factory=list)

    documentation: dict[str, Any] = field(default_factory=dict)
    notifications: dict[str, Any] = field(default_factory=dict)

    def record(
        self,
        phase: LifecyclePhase,
        message: str,
        **detail: Any,
    ) -> TimelineEvent:
        event = TimelineEvent(phase=phase, message=message, detail=detail)
        self.timeline.append(event)
        self.phase = phase
        self.updated_at = event.at
        return event

    @property
    def action_count(self) -> int:
        """Actions actually *executed* (policy-denied candidates don't count).

        Important for the per-incident action cap: burning the cap on
        candidates the Policy Engine rejected would make Sentinel give up
        without ever trying anything.
        """
        return sum(1 for a in self.attempts if a.result is not None)

    @property
    def target_deployment(self) -> str | None:
        """Which deployment this incident is about.

        Prometheus/Loki both label by `app`, and in this repo the `app` label
        value equals the Deployment name for citizen-service,
        notification-service and frontend. That equality is the assumption;
        if it ever stops holding, this property is the single place to fix.
        """
        return self.app

    def to_dict(self, include_evidence: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "fingerprint": self.fingerprint,
            "alertname": self.alertname,
            "severity": self.severity.value,
            "app": self.app,
            "namespace": self.namespace,
            "pod": self.pod,
            "summary": self.summary,
            "description": self.description,
            "labels": self.labels,
            "annotations": self.annotations,
            "customer_id": self.customer_id,
            "environment_id": self.environment_id,
            "application_id": self.application_id,
            "status": self.status.value,
            "phase": self.phase.value,
            "created_at": self.created_at,
            "created_at_iso": iso(self.created_at),
            "updated_at": self.updated_at,
            "updated_at_iso": iso(self.updated_at),
            "resolved_at": self.resolved_at,
            "resolved_at_iso": iso(self.resolved_at) if self.resolved_at else None,
            "firing_count": self.firing_count,
            "failure_class": self.failure_class,
            "alert_fingerprints": self.alert_fingerprints,
            "last_seen_at": self.last_seen_at,
            "suppressed_repeats": self.suppressed_repeats,
            "occurrence": self.occurrence,
            "previous_incident_id": self.previous_incident_id,
            "lifecycle_started_at": self.lifecycle_started_at,
            "reopen_count": self.reopen_count,
            "last_reconsidered_at": self.last_reconsidered_at,
            "evidence_baseline": self.evidence_baseline,
            "hypothesis": self.hypothesis.to_dict() if self.hypothesis else None,
            "attempts": [a.to_dict() for a in self.attempts],
            "timeline": [e.to_dict() for e in self.timeline],
            "deep_proposals": [p.to_dict() for p in self.deep_proposals],
            "deep_investigation_count": self.deep_investigation_count,
            "deep_investigation_traces": [t.to_dict() for t in self.deep_investigation_traces],
            "deep_investigation_running": self.deep_investigation_running,
            "escalated": self.escalated,
            "escalation_reason": self.escalation_reason.value if self.escalation_reason else None,
            "escalation_detail": self.escalation_detail,
            "escalation_record": self.escalation_record,
            "escalation_history": self.escalation_history,
            "documentation": self.documentation,
            "notifications": self.notifications,
        }
        if include_evidence:
            data["evidence"] = self.evidence.to_dict() if self.evidence else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Incident":
        """Reconstruct a live Incident from a stored record — the same shape
        `to_dict()` produces and `SQLiteStore.upsert_incident` persists.

        Nothing in Sentinel needed this until the Sentinel GUI's temporary
        SRE authorization feature: every other code path only ever builds an
        Incident fresh at alert time (routers/alerts.py) and carries it in
        memory for the rest of that one lifecycle run. Granting a temporary
        authorization happens later, against an incident a process may have
        already finished handling and persisted — re-entering the lifecycle
        for it means rebuilding the exact object `to_dict()` flattened, not
        constructing a new one.

        Round-trip fidelity matters concretely, not just tidily: the Policy
        Engine's cooldown and per-incident action-cap checks (see
        lifecycle/policy.py) read `incident.attempts`, so a from_dict that
        silently dropped or mis-typed a field could let a re-entered
        incident sail past a check it would have honestly failed. See
        tests/test_incident_roundtrip.py, which checks this against a real
        incident produced by a real lifecycle run, not a hand-built fixture.
        """
        return cls(
            id=data["id"],
            fingerprint=data["fingerprint"],
            alertname=data["alertname"],
            severity=Severity(data["severity"]),
            app=data.get("app"),
            namespace=data.get("namespace", "citizen-portal"),
            pod=data.get("pod"),
            summary=data.get("summary", ""),
            description=data.get("description", ""),
            labels=data.get("labels") or {},
            annotations=data.get("annotations") or {},
            customer_id=data.get("customer_id"),
            environment_id=data.get("environment_id"),
            application_id=data.get("application_id"),
            status=IncidentStatus(data["status"]),
            phase=LifecyclePhase(data["phase"]),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            resolved_at=data.get("resolved_at"),
            firing_count=data.get("firing_count", 1),
            # Records written before the incident engine work lack these keys;
            # every default below is the neutral "never happened" value.
            failure_class=data.get("failure_class"),
            alert_fingerprints=list(data.get("alert_fingerprints") or []),
            last_seen_at=data.get("last_seen_at", data["updated_at"]),
            suppressed_repeats=data.get("suppressed_repeats", 0),
            occurrence=data.get("occurrence", 1),
            previous_incident_id=data.get("previous_incident_id"),
            lifecycle_started_at=data.get("lifecycle_started_at"),
            reopen_count=data.get("reopen_count", 0),
            last_reconsidered_at=data.get("last_reconsidered_at"),
            evidence_baseline=data.get("evidence_baseline"),
            evidence=Evidence.from_dict(data.get("evidence")),
            hypothesis=Hypothesis.from_dict(data.get("hypothesis")),
            attempts=[AttemptRecord.from_dict(a) for a in data.get("attempts") or []],
            timeline=[TimelineEvent.from_dict(e) for e in data.get("timeline") or []],
            deep_proposals=[
                DeepRemediationProposal.from_dict(p) for p in data.get("deep_proposals") or []
            ],
            deep_investigation_count=data.get("deep_investigation_count", 0),
            deep_investigation_traces=[
                DeepInvestigationTrace.from_dict(t)
                for t in data.get("deep_investigation_traces") or []
            ],
            deep_investigation_running=data.get("deep_investigation_running", False),
            escalated=data.get("escalated", False),
            escalation_reason=(
                EscalationReason(data["escalation_reason"])
                if data.get("escalation_reason")
                else None
            ),
            escalation_detail=data.get("escalation_detail", ""),
            escalation_record=data.get("escalation_record") or {},
            escalation_history=list(data.get("escalation_history") or []),
            documentation=data.get("documentation") or {},
            notifications=data.get("notifications") or {},
            # `started_at_raw` is intentionally never in to_dict()'s output
            # (pre-existing — see its own field), so it stays at its
            # dataclass default here too; nothing regresses because nothing
            # was ever being persisted for it in the first place.
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def iso(epoch: float) -> str:
    """UTC ISO-8601 with a Z suffix, matching the app services' log timestamps."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def new_incident_id() -> str:
    """Human-scannable id: INC-<date>-<8 hex>. Sortable-ish, greppable."""
    return f"INC-{time.strftime('%Y%m%d', time.gmtime())}-{uuid.uuid4().hex[:8]}"


def compute_fingerprint(alertname: str, app: str | None, pod: str | None = None) -> str:
    """Stable dedup key.

    Alertmanager sends its own `fingerprint`, and we prefer it when present
    because it is exactly "this alert instance". When it is absent (hand-made
    payloads, curl testing, a webhook from something that is not
    Alertmanager) we synthesise one from alertname+app. We deliberately do
    NOT include `pod` in the synthesised key: on single-node K3s a restart
    gives the pod a new name, and including it would make every restart look
    like a brand-new incident, defeating dedup during the exact window where
    dedup matters most.
    """
    raw = f"{alertname}|{app or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def compute_incident_key(
    *,
    environment_id: str | None,
    application_id: str | None,
    namespace: str | None,
    app: str | None,
    failure_class: str,
) -> str:
    """Stable INCIDENT identity: "same underlying problem -> same key".

    Inputs are only things that stay true for the life of one problem:
    which environment/application, which namespace/workload, and what CLASS
    of failure (see detection.failure_class_for). Deliberately excluded:

      * timestamps, `startsAt`, metric values, severity — all volatile;
      * the pod name — a restart (Sentinel's own remediation!) renames it;
      * the Alertmanager fingerprint — it hashes every label, pod included;
      * the alertname — two rules describing the same symptom
        (HighHTTPErrorRate + ChaosForcedHTTPFailures) are ONE problem;
      * the RCA category — it is an OUTPUT of the lifecycle (and of an LLM),
        and using it would let a model's opinion decide incident identity.

    Different app => different key, so a citizen-service HTTP 500 and a
    notification-service memory leak can never merge however close in time.
    """
    raw = "|".join(
        [
            environment_id or "",
            application_id or "",
            namespace or "",
            app or "",
            failure_class,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

