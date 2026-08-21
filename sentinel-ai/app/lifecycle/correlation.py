"""
CORRELATION — turn raw evidence into findings.

INVESTIGATION answers "what are the numbers". CORRELATION answers "what do
they mean together". It is pure, synchronous, side-effect-free logic over an
`Evidence` object: no network, no clock reads other than the ones passed in.
That makes it exhaustively unit-testable, which matters because these
findings are what the RCA rules key off.

The single most valuable correlation in this stack is
**deployment-time vs incident-onset**. "Errors started N minutes after a new
ReplicaSet appeared" is the difference between a rollback being obviously
right and being a reckless guess. It is also a hard precondition in the
Policy Engine for rollback, so it gets computed here once and reused.

Everything appended to `evidence.correlations` is a human-readable sentence.
That is intentional: those strings go straight into the RCA narrative, the
incident document and (when enabled) the LLM prompt. Writing them as prose
here means there is exactly one place where the wording lives.
"""
from __future__ import annotations

import logging
import time

from app.models.incident import Evidence, Incident

logger = logging.getLogger(__name__)

# Kubernetes event reasons that indicate a container-level problem. Used
# instead of substring-matching event messages, which change between
# Kubernetes versions.
BAD_EVENT_REASONS = frozenset(
    {
        "BackOff",
        "Failed",
        "FailedCreate",
        "FailedScheduling",
        "FailedMount",
        "Unhealthy",
        "Killing",
        "OOMKilling",
        "Evicted",
        "NodeNotReady",
    }
)

CRASHLOOP_WAITING_REASONS = frozenset(
    {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerError"}
)

# RSS growth over the investigation window that we are willing to call
# leak-shaped. 50 MiB in 30 minutes with no deploy is well outside normal
# jitter for these two small FastAPI processes, whose steady-state RSS is
# roughly 80-150 MiB.
MEMORY_GROWTH_SUSPICIOUS_BYTES = 50 * 1024 * 1024


class CorrelationFindings:
    """Structured conclusions the RCA rules and Policy Engine consume.

    Deliberately a flat bag of booleans and numbers rather than a nested
    structure: every field here is a direct input to an `if` in rca.py or a
    precondition in policy.py, and flatness keeps those readable.
    """

    def __init__(self) -> None:
        # Deployment correlation
        self.recent_deployment: bool = False
        self.recent_deployment_age_seconds: float | None = None
        self.deploy_correlates_with_onset: bool = False
        self.previous_revision: int | None = None
        self.current_revision: int | None = None
        self.revision_count: int = 0
        self.image_changed: bool = False

        # Symptom shape
        self.error_spike: bool = False
        self.latency_spike: bool = False
        self.service_down: bool = False
        self.crash_looping: bool = False
        self.memory_growth_suspicious: bool = False
        self.cpu_saturated: bool = False
        self.replicas_unavailable: bool = False
        self.capacity_pressure: bool = False

        # Chaos
        self.chaos_db_fault: bool = False
        self.chaos_error_fault: bool = False
        self.chaos_latency_fault: bool = False
        self.chaos_notification_fault: bool = False
        self.chaos_pods_affected: list[str] = []

        # Downstream
        self.downstream_degraded: bool = False
        self.notification_delivery_failing: bool = False

        # Evidence-quality flags. RCA lowers its confidence when these are
        # set, because a hypothesis built on missing data deserves less
        # confidence, and confidence is what unlocks autonomous action.
        self.metrics_missing: bool = False
        self.logs_missing: bool = False
        self.k8s_missing: bool = False

    def to_dict(self) -> dict[str, object]:
        return dict(self.__dict__)


def correlate(
    incident: Incident,
    evidence: Evidence,
    correlation_window_minutes: int,
    cpu_threshold_cores: float,
    error_rate_threshold: float,
    p95_threshold_seconds: float,
    now: float | None = None,
) -> CorrelationFindings:
    """Derive findings. Pure function — `now` is injectable for tests."""
    now = now if now is not None else time.time()
    f = CorrelationFindings()

    # ---- evidence quality ----------------------------------------------
    f.metrics_missing = evidence.error_rate is None and evidence.up is None
    f.logs_missing = any("loki" in e.lower() for e in evidence.errors) or any(
        "collector 'errors'" in e for e in evidence.errors
    )
    f.k8s_missing = evidence.deployment is None

    # ---- deployment history --------------------------------------------
    history = [r for r in evidence.replicaset_history if r.get("revision") is not None]
    f.revision_count = len(history)
    if history:
        newest = history[0]
        f.current_revision = newest.get("revision")
        created = newest.get("created_at")
        if created:
            age = max(0.0, now - created)
            f.recent_deployment_age_seconds = age
            f.recent_deployment = age <= correlation_window_minutes * 60
        if len(history) > 1:
            f.previous_revision = history[1].get("revision")
            newest_images = set(newest.get("images") or [])
            previous_images = set(history[1].get("images") or [])
            # An image change is the strongest signal that a rollback would
            # actually change what is running. A revision bump with identical
            # images is usually a `rollout restart` (annotation-only change)
            # — possibly one Sentinel itself performed a minute ago, which is
            # exactly the case where rolling "back" achieves nothing.
            f.image_changed = bool(newest_images != previous_images)

    # ---- symptom shape --------------------------------------------------
    if evidence.error_rate is not None and evidence.error_rate > error_rate_threshold:
        f.error_spike = True
        evidence.correlations.append(
            f"5xx error rate is {evidence.error_rate:.1%}, above the "
            f"{error_rate_threshold:.1%} threshold"
        )
    if (
        evidence.p95_latency_seconds is not None
        and evidence.p95_latency_seconds > p95_threshold_seconds
    ):
        f.latency_spike = True
        evidence.correlations.append(
            f"p95 latency is {evidence.p95_latency_seconds:.2f}s, above the "
            f"{p95_threshold_seconds:.2f}s threshold"
        )
    if evidence.up is not None and evidence.up == 0:
        f.service_down = True
        evidence.correlations.append(
            'up{job="kubernetes-pods"} is 0 for this app: Prometheus cannot '
            "scrape at least one pod"
        )

    for pod in evidence.pods:
        for container in pod.get("container_states") or []:
            waiting = container.get("waiting_reason")
            if waiting in CRASHLOOP_WAITING_REASONS:
                f.crash_looping = True
                evidence.correlations.append(
                    f"pod {pod.get('name')} container {container.get('name')} is "
                    f"waiting with reason {waiting}"
                )
            if container.get("last_terminated_reason") == "OOMKilled":
                # OOMKill is the one memory signal that does not depend on
                # process_resident_memory_bytes sampling luck.
                f.memory_growth_suspicious = True
                evidence.correlations.append(
                    f"pod {pod.get('name')} container {container.get('name')} was "
                    "previously OOMKilled"
                )

    if (
        evidence.memory_growth_bytes is not None
        and evidence.memory_growth_bytes > MEMORY_GROWTH_SUSPICIOUS_BYTES
    ):
        f.memory_growth_suspicious = True
        evidence.correlations.append(
            f"resident memory grew by "
            f"{evidence.memory_growth_bytes / (1024 * 1024):.0f} MiB over the "
            "observation window"
        )

    if evidence.cpu_cores is not None and evidence.cpu_cores > cpu_threshold_cores:
        f.cpu_saturated = True
        evidence.correlations.append(
            f"process CPU is {evidence.cpu_cores:.2f} cores, above the "
            f"{cpu_threshold_cores:.2f} core threshold. Note: this is "
            "process_cpu_seconds_total from prometheus_client, not a cgroup "
            "metric — there is no cAdvisor here, so this cannot be compared "
            "against the container's CPU limit."
        )

    deployment = evidence.deployment or {}
    desired = deployment.get("desired_replicas")
    available = deployment.get("available_replicas")
    if desired is not None and available is not None and available < desired:
        f.replicas_unavailable = True
        evidence.correlations.append(
            f"deployment has {available}/{desired} replicas available"
        )

    # Capacity pressure: everything is *up* and healthy-looking, but slow and
    # busy. This is the one shape where scaling is the right answer rather
    # than restarting; restarting a saturated service just removes capacity.
    if (
        f.latency_spike
        and not f.error_spike
        and not f.crash_looping
        and not f.replicas_unavailable
        and evidence.cpu_cores is not None
        and evidence.cpu_cores > cpu_threshold_cores * 0.6
    ):
        f.capacity_pressure = True
        evidence.correlations.append(
            "latency is elevated with high CPU but no errors and no unhealthy "
            "pods, which is the shape of capacity pressure rather than a fault"
        )

    # ---- chaos ----------------------------------------------------------
    for pod, metrics in (evidence.chaos_state or {}).items():
        affected = False
        if metrics.get("chaos_db_failure", 0.0) > 0:
            f.chaos_db_fault = True
            affected = True
        if metrics.get("chaos_error_rate", 0.0) > 0:
            f.chaos_error_fault = True
            affected = True
        if metrics.get("chaos_latency_ms", 0.0) > 0:
            f.chaos_latency_fault = True
            affected = True
        if (metrics.get("chaos_notification_failure_rate") or 0.0) > 0:
            f.chaos_notification_fault = True
            affected = True
        if affected:
            f.chaos_pods_affected.append(pod)

    if f.chaos_pods_affected:
        evidence.correlations.append(
            "active chaos faults found on pod(s) "
            + ", ".join(sorted(f.chaos_pods_affected))
            + " via the chaos_* gauges. Chaos state is per-pod in-memory, so the "
            "fault must be cleared on the specific pod and re-verified per "
            "kubernetes_pod_name."
        )

    # `chaos_injections_total` shows faults that *fired* in the window, which
    # can be non-zero even after the gauge was cleared. Useful history, not
    # proof of a current fault — so it never sets a chaos_*_fault flag.
    if evidence.chaos_injections:
        fired = ", ".join(
            f"{k}={v:.0f}" for k, v in sorted(evidence.chaos_injections.items()) if v > 0
        )
        if fired:
            evidence.correlations.append(
                f"chaos_injections_total increased in the window ({fired}). This is "
                "history: a non-zero increase with all chaos_* gauges at 0 means the "
                "fault already ended."
            )

    # ---- downstream -----------------------------------------------------
    # The health endpoint returns HTTP 200 with {"status":"degraded"} when
    # notification-service is unreachable. A status-code-only check sees a
    # perfectly healthy service. This is why validation parses the JSON body.
    if evidence.health_status == "degraded":
        f.downstream_degraded = True
        evidence.correlations.append(
            "/readyz returned HTTP 200 with status=degraded: the service itself is "
            "healthy but a downstream dependency is not reachable. A status-code-only "
            "health check would have shown this as fully healthy."
        )
    if evidence.health_status == "not_ready":
        evidence.correlations.append(
            "/readyz returned status=not_ready, which in this stack means the "
            "database check failed (HTTP 503)."
        )

    failed = evidence.notification_deliveries.get("Failed", 0.0)
    sent = evidence.notification_deliveries.get("Sent", 0.0)
    if failed > 0 and (failed / max(failed + sent, 1.0)) > 0.2:
        f.notification_delivery_failing = True
        evidence.correlations.append(
            f"notification_deliveries_total shows {failed:.0f} Failed vs {sent:.0f} "
            "Sent in the window (note: this metric's result label is capitalised)"
        )
    if (evidence.notification_dispatch_failures or 0.0) > 0:
        evidence.correlations.append(
            f"citizen-service recorded "
            f"{evidence.notification_dispatch_failures:.0f} failed dispatches to "
            "notification-service (notification_dispatches_total{result=\"failure\"}), "
            "which is the caller's view rather than the delivery view"
        )

    # ---- the deployment/onset correlation, stated explicitly ------------
    # This is the gate for rollback. We require BOTH a recent deployment AND
    # a symptom that a rollback could plausibly fix. A new ReplicaSet plus a
    # chaos fault is not a bad deploy — it is a chaos fault that happened to
    # follow a deploy, and rolling back would neither clear the fault nor be
    # honest about the cause.
    symptom_present = (
        f.error_spike or f.latency_spike or f.crash_looping or f.replicas_unavailable
    )
    chaos_present = (
        f.chaos_db_fault
        or f.chaos_error_fault
        or f.chaos_latency_fault
        or f.chaos_notification_fault
    )
    f.deploy_correlates_with_onset = bool(
        f.recent_deployment and symptom_present and not chaos_present
    )
    if f.deploy_correlates_with_onset:
        age_min = (f.recent_deployment_age_seconds or 0) / 60
        evidence.correlations.append(
            f"a new ReplicaSet (revision {f.current_revision}) appeared "
            f"{age_min:.1f} minutes ago, inside the "
            f"{correlation_window_minutes} minute correlation window, and the "
            "symptoms are consistent with a bad deployment"
        )
    elif f.recent_deployment and chaos_present:
        evidence.correlations.append(
            f"a deployment happened recently (revision {f.current_revision}) but "
            "an active chaos fault is also present, so the deployment is not "
            "treated as the cause and rollback is not correlated"
        )

    for line in evidence.k8s_events:
        if line.get("reason") in BAD_EVENT_REASONS:
            evidence.correlations.append(
                f"kubernetes event {line.get('reason')} on {line.get('object')}: "
                f"{(line.get('message') or '')[:160]}"
            )

    logger.info(
        "correlation_complete",
        extra={
            "app": incident.app,
            "error_spike": f.error_spike,
            "recent_deployment": f.recent_deployment,
            "deploy_correlates": f.deploy_correlates_with_onset,
            "chaos_active": bool(f.chaos_pods_affected),
        },
    )
    return f
