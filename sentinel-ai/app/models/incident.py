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
        """Strict parse used on the LLM boundary."""
        if not isinstance(raw, str):
            return None

        # Reject control/whitespace characters inside or around the value.
        if raw != raw.strip() or any(char in raw for char in "\r\n\t"):
            return None

        candidate = raw.lower()

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


class EscalationReason(str, Enum):
    NO_SAFE_ACTION = "no_safe_action"
    ACTION_CAP_REACHED = "action_cap_reached"
    VALIDATION_FAILED = "validation_failed"
    STATEFUL_TARGET = "stateful_target"
    REMEDIATION_ERROR = "remediation_error"
    LOW_CONFIDENCE = "low_confidence"
    UNKNOWN_ALERT = "unknown_alert"
    INTERNAL_ERROR = "internal_error"


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

    # Kubernetes
    deployment: dict[str, Any] | None = None
    pods: list[dict[str, Any]] = field(default_factory=list)
    k8s_events: list[dict[str, Any]] = field(default_factory=list)
    replicaset_history: list[dict[str, Any]] = field(default_factory=list)
    restart_count_total: int = 0
    latest_revision_age_seconds: float | None = None

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
            "health_status": self.health_status,
            "health_http_code": self.health_http_code,
            "health_checks": self.health_checks,
            "correlations": self.correlations,
            "errors": self.errors,
        }


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
        }


@dataclass
class PolicyVerdict:
    allowed: bool
    action: RemediationAction
    reason: DenialReason | None = None
    detail: str = ""
    # Policy may *narrow* a plan (e.g. clamp a scale target into the band).
    adjusted_params: ActionParams | None = None
    checks: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "action": self.action.value,
            "reason": self.reason.value if self.reason else None,
            "detail": self.detail,
            "adjusted_params": self.adjusted_params.to_dict() if self.adjusted_params else None,
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

    status: IncidentStatus = IncidentStatus.OPEN
    phase: LifecyclePhase = LifecyclePhase.DETECTION
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    resolved_at: float | None = None
    started_at_raw: str | None = None

    # How many distinct alert firings folded into this incident.
    firing_count: int = 1

    evidence: Evidence | None = None
    hypothesis: Hypothesis | None = None
    attempts: list[AttemptRecord] = field(default_factory=list)
    timeline: list[TimelineEvent] = field(default_factory=list)

    escalated: bool = False
    escalation_reason: EscalationReason | None = None
    escalation_detail: str = ""

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
            "status": self.status.value,
            "phase": self.phase.value,
            "created_at": self.created_at,
            "created_at_iso": iso(self.created_at),
            "updated_at": self.updated_at,
            "updated_at_iso": iso(self.updated_at),
            "resolved_at": self.resolved_at,
            "resolved_at_iso": iso(self.resolved_at) if self.resolved_at else None,
            "firing_count": self.firing_count,
            "hypothesis": self.hypothesis.to_dict() if self.hypothesis else None,
            "attempts": [a.to_dict() for a in self.attempts],
            "timeline": [e.to_dict() for e in self.timeline],
            "escalated": self.escalated,
            "escalation_reason": self.escalation_reason.value if self.escalation_reason else None,
            "escalation_detail": self.escalation_detail,
            "documentation": self.documentation,
            "notifications": self.notifications,
        }
        if include_evidence:
            data["evidence"] = self.evidence.to_dict() if self.evidence else None
        return data


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
