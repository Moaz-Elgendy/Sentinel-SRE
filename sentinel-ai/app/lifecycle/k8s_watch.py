"""
KUBERNETES DESIRED-STATE WATCH — detect unhealthy workloads independently of
Alertmanager.

### Why this exists

Detection was previously 100% dependent on `POST /api/alerts/webhook`
(`app/routers/alerts.py`) — nothing in this codebase ever inspected
Kubernetes on its own. That is fine for signals that genuinely only exist as
metrics (error rate, latency). It is NOT fine for "the Deployment has no
Pods", because the only rule that could plausibly catch that
(`ServiceDown: up{...} == 0`, in `k8s/monitoring/prometheus/rules-configmap.yaml`)
depends on Prometheus's Kubernetes pod-role service discovery having found a
Pod to scrape in the first place. When `spec.replicas == 0`, or every Pod a
Deployment creates crash-loops and gets killed before a scrape ever lands,
there is no scrape target — `up` for that workload is *absent*, not `0`, and
`up{...} == 0` can never fire on an absent series. No `kube-state-metrics` is
deployed either, so there is also no `kube_deployment_status_replicas_*`-style
signal Prometheus could alert on instead. The result: a Deployment can sit at
desired=0/available=0/no-Pod indefinitely and Sentinel does nothing, because
nothing ever told it to look.

### What this module does, and does not, decide

This module's ONLY job is: notice a mismatch between a Deployment's desired
and available replica counts, and — once that mismatch has persisted past a
debounce window — hand a normalised alert dict to the SAME
`detection`/`IncidentManager` path `routers/alerts.py` already uses. It does
not build its own incident, does not call RCA, does not decide remediation,
and does not bypass deduplication, correlation, the ESCALATED/RESOLVED state
machine, or the policy engine — all of that is exactly the same code a real
Alertmanager alert would run through. This is a second *sensor*, not a
second detection system.

### Desired = 0: intentional scale-down vs. an unhealthy workload

`spec.replicas == 0` is ambiguous on its own — it is also what a legitimate
scale-to-zero looks like. Sentinel had no existing concept of "expected"
workload state to disambiguate this (see the investigation report this
module was written from). Inventing a hardcoded rule for one application
would be wrong, so instead: a Deployment carrying the annotation named by
`settings.k8s_watch_expected_zero_annotation` (default
`sentinel.sre/expected-scale-zero: "true"`) is treated as an intentional
zero and is never flagged for that reason alone. Any other Deployment sitting
at desired=0 with no Pods, for longer than the debounce window, is treated
the same as any other unavailable workload — Sentinel investigates it like
it would investigate any other alert, including the possibility that RCA and
a human conclude "this was fine". Silence is not neutral: not raising an
incident IS a decision, and this project has no evidence that a Deployment
sitting broken is ever the *safe* default to assume.

Desired > 0 with available < desired (crash-looping, no Pod ever created,
rollout stuck) is unambiguous and is always eligible once debounced.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.lifecycle import detection
from app.models.incident import Severity

logger = logging.getLogger(__name__)

WATCH_ALERTNAME = "DeploymentUnavailable"


class _DeploymentWatchState:
    """Per-Deployment debounce/notification bookkeeping. In-memory only —
    a restart re-derives everything from the next poll, which is correct:
    the debounce window just restarts, it does not lose the underlying
    Kubernetes state."""

    __slots__ = ("unhealthy_since", "notified", "reason")

    def __init__(self) -> None:
        self.unhealthy_since: float | None = None
        self.notified: bool = False
        self.reason: str = ""


def _is_unhealthy(
    dep: dict[str, Any], expected_zero_annotation: str
) -> tuple[bool, str]:
    """Return (unhealthy, reason). `dep` is a `list_deployments()`-shaped
    dict; annotations are only present when the caller fetched full detail
    (see `_evaluate` below) since `list_deployments()` itself does not
    return them.
    """
    desired = dep.get("desired_replicas")
    available = dep.get("available_replicas") or 0
    if desired is None:
        return False, ""
    if desired == 0:
        annotations = dep.get("annotations") or {}
        if str(annotations.get(expected_zero_annotation, "")).lower() == "true":
            return False, ""
        if available > 0:
            # Scaling down; not yet actually zero. Nothing to flag.
            return False, ""
        return True, (
            "Deployment is scaled to 0 desired replicas with no annotation "
            f"marking this intentional ({expected_zero_annotation}=true is absent)"
        )
    if available < desired:
        return True, (
            f"Deployment desired={desired} but only available={available}; "
            "no Pod is providing the desired capacity"
        )
    return False, ""


def _normalised_alert(
    *, alertname: str, app: str, namespace: str, status: str, reason: str
) -> dict[str, Any]:
    """Build the exact dict shape `app.lifecycle.detection.normalise_alert()`
    produces, so it can be handed to `IncidentManager.handle_alert()` /
    `.handle_resolved()` exactly as `routers/alerts.py` does for a real
    Alertmanager payload. Keeping the shape identical (rather than adding a
    parallel code path) is what keeps this a sensor and not a second
    detection system.
    """
    return {
        "alertname": alertname,
        "severity": Severity.CRITICAL,
        "app": app,
        "namespace": namespace,
        "pod": None,
        "summary": f"{app} is unavailable",
        "description": reason,
        "labels": {"app": app, "kubernetes_namespace": namespace, "source": "k8s_watch"},
        "annotations": {"summary": f"{app} is unavailable", "description": reason},
        "fingerprint": f"k8s-watch:{namespace}:{app}",
        "status": status,
        "startsAt": None,
    }


async def _reconcile_stale_pod_incidents(
    ctx: Any,
    incident_manager: Any,
    environment: Any,
    deployments: list[dict[str, Any]],
) -> None:
    """Auto-resolve availability incidents whose recorded Pod disappeared.

    Alertmanager identifies a Pod-level alert by the Pod name/fingerprint. A
    normal Deployment replacement creates a new Pod, so the old alert may lose
    its scrape target without Sentinel receiving a useful resolved webhook.
    This reconciliation closes that stale incident only when ALL of these are
    true:

    * the incident is still non-terminal and records a Pod name;
    * it is an availability incident (not a memory/CPU/latency diagnosis);
    * the target Deployment still wants replicas and currently has all desired
      replicas available; and
    * the recorded Pod name is gone while at least one current Pod is Ready.

    The exact incident id is passed to IncidentManager so a resolution cannot
    accidentally close a newer occurrence that shares the same workload and
    failure class.
    """
    store = getattr(incident_manager, "store", None)
    if store is None:
        return

    try:
        records = store.list_by_statuses(_RECONCILABLE_STATUSES)
    except Exception:  # noqa: BLE001 - reconciliation must not kill the watch
        logger.exception("k8s_watch_incident_list_failed")
        return

    namespace = environment.kubernetes.namespace
    by_name = {d.get("name"): d for d in deployments if d.get("name")}
    candidates: dict[str, list[dict[str, Any]]] = {}
    for incident in records:
        if incident.get("namespace") != namespace:
            continue
        if not incident.get("pod"):
            continue
        if incident.get("alertname") not in POD_AVAILABILITY_ALERTNAMES:
            continue
        app = incident.get("app")
        if not app or app not in by_name:
            continue
        candidates.setdefault(app, []).append(incident)

    for app, incidents in candidates.items():
        dep = by_name[app]
        desired = dep.get("desired_replicas")
        available = dep.get("available_replicas") or 0
        if desired is None or desired <= 0 or available < desired:
            continue

        try:
            pods = await ctx.k8s.list_pods(namespace, label_selector=f"app={app}")
        except Exception:  # noqa: BLE001
            logger.exception(
                "k8s_watch_list_pods_for_reconciliation_failed",
                extra={"namespace": namespace, "deployment": app},
            )
            continue

        current_names = {p.get("name") for p in pods if p.get("name")}
        ready = any(p.get("ready") for p in pods)
        if not ready:
            continue

        for incident in incidents:
            pod = incident.get("pod")
            if not pod or pod in current_names:
                continue
            reason = (
                f"Recorded Pod {namespace}/{pod} no longer exists; Deployment "
                f"{namespace}/{app} now has {available}/{desired} available replicas "
                "and a Ready replacement Pod. Treating the stale Pod-level "
                "availability condition as cleared."
            )
            try:
                result = incident_manager.auto_resolve_incident(
                    incident["id"], reason=reason, source="k8s_watch"
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "k8s_watch_auto_resolution_failed",
                    extra={"incident_id": incident.get("id"), "deployment": app, "pod": pod},
                )
                continue
            if result.get("resolved"):
                logger.info(
                    "k8s_watch_stale_pod_incident_auto_resolved",
                    extra={
                        "incident_id": incident.get("id"),
                        "deployment": app,
                        "pod": pod,
                    },
                )


async def _evaluate(
    ctx: Any,
    incident_manager: Any,
    environment: Any,
    state: dict[str, _DeploymentWatchState],
    debounce_seconds: float,
    expected_zero_annotation: str,
) -> None:
    namespace = environment.kubernetes.namespace
    try:
        deployments = await ctx.k8s.list_deployments(namespace)
    except Exception:  # noqa: BLE001 - a poll failure must not kill the loop
        logger.exception("k8s_watch_list_deployments_failed", extra={"namespace": namespace})
        return

    seen_names: set[str] = set()
    now = time.time()

    # Reconcile Pod-level availability incidents independently of Alertmanager.
    # This is deliberately done before the Deployment-unavailable loop: the
    # current Deployment can be healthy even though the incident's recorded
    # Pod object has already been replaced.
    await _reconcile_stale_pod_incidents(ctx, incident_manager, environment, deployments)

    for dep in deployments:
        name = dep.get("name")
        if not name:
            continue
        seen_names.add(name)
        desired = dep.get("desired_replicas")

        # Only pay for the extra read-only call (annotations) on the one
        # ambiguous case — desired == 0 — where it can change the verdict.
        # This keeps the common per-poll cost at one list call regardless of
        # namespace size.
        if desired == 0:
            full = await ctx.k8s.get_deployment(namespace, name)
            if full is not None:
                dep = {**dep, "annotations": full.get("annotations", {})}

        unhealthy, reason = _is_unhealthy(dep, expected_zero_annotation)
        st = state.setdefault(name, _DeploymentWatchState())

        if unhealthy:
            if st.unhealthy_since is None:
                st.unhealthy_since = now
                st.notified = False
                st.reason = reason
            elif reason != st.reason:
                # The condition changed shape (e.g. crash-loop -> scaled to
                # zero) — restart the debounce rather than silently
                # relabelling an already-open window.
                st.unhealthy_since = now
                st.notified = False
                st.reason = reason

            if not st.notified and (now - st.unhealthy_since) >= debounce_seconds:
                normalised = _normalised_alert(
                    alertname=WATCH_ALERTNAME,
                    app=name,
                    namespace=namespace,
                    status="firing",
                    reason=st.reason,
                )
                actionable, skip_reason = detection.is_actionable(normalised)
                if actionable:
                    incident_manager.handle_alert(normalised, environment)
                    logger.info(
                        "k8s_watch_incident_signalled",
                        extra={"deployment": name, "namespace": namespace, "reason": st.reason},
                    )
                else:
                    logger.info(
                        "k8s_watch_alert_not_actionable",
                        extra={"deployment": name, "skip_reason": skip_reason},
                    )
                st.notified = True
        else:
            if st.notified:
                # We previously told the IncidentManager this Deployment was
                # down; tell it that condition cleared, through the exact
                # same resolved-alert path a real Alertmanager "resolved"
                # webhook uses, so dedup/state-machine rules apply
                # identically either way.
                normalised = _normalised_alert(
                    alertname=WATCH_ALERTNAME,
                    app=name,
                    namespace=namespace,
                    status="resolved",
                    reason="desired and available replica counts now match",
                )
                incident_manager.handle_resolved(normalised, environment)
            st.unhealthy_since = None
            st.notified = False
            st.reason = ""

    # A Deployment that disappeared entirely (deleted) between polls: drop
    # its state rather than let it grow unbounded. It is not this module's
    # job to decide whether a deleted Deployment is itself a problem.
    for stale_name in set(state) - seen_names:
        del state[stale_name]


async def run_k8s_watch(
    ctx: Any,
    incident_manager: Any,
    environment: Any,
    *,
    poll_interval_seconds: float,
    debounce_seconds: float,
    expected_zero_annotation: str,
) -> None:
    """The long-running poll loop. Intended to be wrapped in an
    `asyncio.create_task` from `main.py`'s lifespan and cancelled at
    shutdown — a `CancelledError` from that is expected and not logged as a
    failure.

    Every iteration is independent and swallows its own errors (see
    `_evaluate`) so one bad poll (a transient API-server hiccup) does not
    stop future polls from running; only the availability of `ctx.k8s`
    itself decides whether this watch can do anything.
    """
    state: dict[str, _DeploymentWatchState] = {}
    logger.info(
        "k8s_watch_started",
        extra={
            "namespace": environment.kubernetes.namespace,
            "poll_interval_seconds": poll_interval_seconds,
            "debounce_seconds": debounce_seconds,
        },
    )
    try:
        while True:
            if ctx.k8s.available:
                await _evaluate(
                    ctx,
                    incident_manager,
                    environment,
                    state,
                    debounce_seconds,
                    expected_zero_annotation,
                )
            await asyncio.sleep(poll_interval_seconds)
    except asyncio.CancelledError:
        logger.info("k8s_watch_stopped")
        raise
