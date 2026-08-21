"""
INVESTIGATION — gather an evidence bundle.

This phase is strictly read-only and strictly parallel. Every collector runs
concurrently via `asyncio.gather(..., return_exceptions=True)` because an
incident is a latency-sensitive situation: serialising eight HTTP round trips
against Prometheus, Loki and the API server would add seconds to
time-to-remediation for no benefit.

`return_exceptions=True` is load-bearing, not laziness. If Loki is down —
which is entirely plausible during an incident — we still want the metric and
Kubernetes evidence. Each failure is appended to `Evidence.errors` so that
RCA and the incident document can say "we could not see the logs" rather than
implying the logs were clean. A remediator that cannot distinguish "no errors"
from "no data" will eventually declare an outage resolved.

RE-INVESTIGATION after a failed remediation calls the exact same function.
There is no separate code path; the only difference is the phase label on the
timeline entry, which the orchestrator supplies.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.clients.kubernetes_client import KubernetesClient
from app.clients.loki import LokiClient, looks_like_chaos_silence, summarise
from app.clients.prometheus import PrometheusClient
from app.models.incident import Evidence, Incident

logger = logging.getLogger(__name__)

# How far back the evidence window reaches. 15 minutes covers the typical
# `for:` duration on the existing alert rules (2-5m) plus enough lead-in to
# see what changed just before the alert started firing.
LOOKBACK_SECONDS = 900


async def investigate(
    incident: Incident,
    prom: PrometheusClient,
    loki: LokiClient,
    k8s: KubernetesClient,
    health_probe: Any = None,
) -> Evidence:
    """Collect everything we can see about this incident.

    `health_probe` is an async callable ``(deployment) -> dict`` (normally
    validation.probe_health). It is injected rather than imported so tests can
    run this whole phase without a network stack.
    """
    evidence = Evidence()
    app = incident.app
    namespace = incident.namespace
    now = time.time()
    start = now - LOOKBACK_SECONDS

    # ---- metric collectors ---------------------------------------------
    metric_tasks: dict[str, Any] = {
        "error_rate": prom.error_rate(app),
        "error_count": prom.error_count(app),
        "request_rate": prom.request_rate(app),
        "p95": prom.p95_latency(app),
        "cpu": prom.cpu_cores(app),
        "memory": prom.memory_bytes(app),
        "memory_growth": prom.memory_growth_bytes(app),
        "up": prom.up(app),
        "chaos_state": prom.chaos_state(app),
        "chaos_injections": prom.chaos_injections(app),
        "notification_deliveries": prom.notification_deliveries(),
        "notification_dispatch_failures": prom.notification_dispatch_failures(),
    }

    # ---- log collectors -------------------------------------------------
    log_tasks: dict[str, Any] = {
        "errors": loki.recent_errors(app, namespace, start, now),
        "access_5xx": loki.access_log_errors(app, namespace, start, now),
    }

    # ---- kubernetes collectors -----------------------------------------
    k8s_tasks: dict[str, Any] = {}
    target = incident.target_deployment
    if k8s.available and target:
        k8s_tasks = {
            "deployment": k8s.get_deployment(namespace, target),
            # Pods are selected by the `app` label. In this repo the
            # Deployments' pod templates carry `app: <service-name>`, which is
            # also the Prometheus `app` label value — the same equality the
            # Incident.target_deployment property relies on.
            "pods": k8s.list_pods(namespace, label_selector=f"app={target}"),
            "events": k8s.list_events(namespace, since_seconds=LOOKBACK_SECONDS * 2),
            "replicasets": k8s.list_replicasets(namespace, target),
        }
    elif not k8s.available:
        evidence.errors.append(
            f"kubernetes API unavailable ({k8s.init_error or 'not initialised'}); "
            "no deployment, pod, event or revision evidence"
        )
    elif not target:
        evidence.errors.append(
            "alert carried no identifiable app/deployment label; Kubernetes "
            "evidence skipped and no deployment-targeted action is possible"
        )

    health_task: dict[str, Any] = {}
    if health_probe is not None and target:
        health_task = {"health": health_probe(target)}

    all_tasks = {**metric_tasks, **log_tasks, **k8s_tasks, **health_task}
    keys = list(all_tasks.keys())
    results = await asyncio.gather(*(all_tasks[k] for k in keys), return_exceptions=True)
    collected: dict[str, Any] = {}
    for key, value in zip(keys, results, strict=True):
        if isinstance(value, BaseException):
            evidence.errors.append(f"collector '{key}' failed: {str(value)[:200]}")
            collected[key] = None
        else:
            collected[key] = value

    # ---- fold results into the bundle ----------------------------------
    evidence.error_rate = collected.get("error_rate")
    evidence.error_rate_5xx_count = collected.get("error_count")
    evidence.request_rate = collected.get("request_rate")
    evidence.p95_latency_seconds = collected.get("p95")
    evidence.cpu_cores = collected.get("cpu")
    evidence.memory_bytes = collected.get("memory")
    evidence.memory_growth_bytes = collected.get("memory_growth")
    evidence.up = collected.get("up")
    evidence.chaos_state = collected.get("chaos_state") or {}
    evidence.chaos_injections = collected.get("chaos_injections") or {}
    evidence.notification_deliveries = collected.get("notification_deliveries") or {}
    evidence.notification_dispatch_failures = collected.get(
        "notification_dispatch_failures"
    )

    error_lines = collected.get("errors") or []
    evidence.log_lines = error_lines
    evidence.log_error_count = len(error_lines)
    evidence.log_sample_messages = summarise(error_lines)
    access_5xx = collected.get("access_5xx") or []
    evidence.access_log_line_count = len(access_5xx)

    evidence.deployment = collected.get("deployment")
    evidence.pods = collected.get("pods") or []
    evidence.k8s_events = collected.get("events") or []
    evidence.replicaset_history = collected.get("replicasets") or []
    evidence.restart_count_total = sum(
        int(p.get("restart_count") or 0) for p in evidence.pods
    )

    history = evidence.replicaset_history
    if history:
        newest = history[0]
        created = newest.get("created_at")
        if created:
            evidence.latest_revision_age_seconds = max(0.0, now - created)

    health = collected.get("health") or {}
    if isinstance(health, dict):
        evidence.health_status = health.get("status")
        evidence.health_http_code = health.get("http_code")
        evidence.health_checks = health.get("checks") or {}

    # ---- one derived observation that belongs here ----------------------
    # We compute this in INVESTIGATION rather than RCA because it is a
    # statement about the evidence-collection itself: metrics saw 5xx, logs
    # saw none. See clients/loki.py for why that combination is expected
    # under chaos injection and is NOT a broken log pipeline.
    if looks_like_chaos_silence(evidence.error_rate, evidence.access_log_line_count):
        evidence.correlations.append(
            "metrics show a 5xx rate but Loki has zero matching access-log lines "
            "with status_code>=500. The chaos middleware short-circuits before the "
            "access-log middleware, so chaos-injected 503s are counted but never "
            "logged. This absence of logs is expected under chaos injection and is "
            "itself evidence of a deliberate fault rather than an application error."
        )

    logger.info(
        "investigation_complete",
        extra={
            "app": app,
            "error_rate": evidence.error_rate,
            "p95_latency_seconds": evidence.p95_latency_seconds,
            "up": evidence.up,
            "collector_errors": len(evidence.errors),
            "log_error_count": evidence.log_error_count,
        },
    )
    return evidence
