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

from app.clients.github_client import GitHubClient
from app.clients.kubernetes_client import KubernetesClient
from app.clients.loki import LokiClient, looks_like_chaos_silence, summarise
from app.clients.prometheus import PrometheusClient
from app.lifecycle.correlation import CRASHLOOP_WAITING_REASONS
from app.models.incident import Evidence, Incident

logger = logging.getLogger(__name__)

# How far back the evidence window reaches. 15 minutes covers the typical
# `for:` duration on the existing alert rules (2-5m) plus enough lead-in to
# see what changed just before the alert started firing.
#
# This is the "MAX_METRIC_RANGE" / "MAX_LOG_LINES" bound a hypothesis-driven,
# multi-round investigator would need to configure per tool call. This
# implementation is deterministic and single-round instead (see
# MAX_INVESTIGATION_ROUNDS below), so the equivalent bounds already exist —
# just as real default parameters on the collectors themselves, not a
# separate config surface:
#   get_metrics        -> PrometheusClient.*(app) queries this LOOKBACK_SECONDS
#                          window; each call has its own httpx timeout
#                          (PrometheusClient(timeout=...), default 10s).
#   query_logs         -> LokiClient.recent_errors/access_log_errors, each
#                          bounded by its own `limit` kwarg (100-200 lines)
#                          and the same per-call timeout.
#   inspect_pods,
#   inspect_deployment,
#   inspect_events,
#   inspect_previous_revision
#                      -> KubernetesClient.list_pods/get_deployment/
#                          list_events/list_replicasets, each a single bounded
#                          API list/get call (list_events is itself bounded by
#                          `since_seconds`).
#   inspect_service_health -> the injected `health_probe` (see
#                          validation.probe_health), its own httpx timeout.
#   inspect_dependencies -> the notification_deliveries/dispatch_failures
#                          Prometheus queries, scoped by owner app (see the
#                          evidence-isolation comment below) so one
#                          incident's dependency evidence can never leak into
#                          another's.
LOOKBACK_SECONDS = 900

# Every real incident today runs exactly one investigation round: all
# collectors above fire concurrently, and re-investigation (after a failed
# validation, or a manual/automatic reopen) replays this same function in
# full rather than adaptively picking a next tool call from a partial
# result. That is a deliberate choice (see this module's docstring and the
# brief this constant was added for): a complete, structured evidence bundle
# every time is simpler to test, cheaper (one RCA/LLM call per investigation,
# not N), and easier to audit than an adaptive multi-round loop would be.
# Named and set to 1 here, rather than left as an implicit assumption of the
# control flow, so a future adaptive investigator has an explicit switch to
# change rather than a hidden one to discover.
MAX_INVESTIGATION_ROUNDS = 1

# The app that emits `notification_deliveries_total` (its own delivery view)
# and the app that emits `notification_dispatches_total` (the caller's view).
NOTIFICATION_OWNER_APP = "notification-service"
NOTIFICATION_CALLER_APP = "citizen-service"


def _extract_sha_from_image(image: str) -> str | None:
    """"<registry>/<prefix>/<service>:<tag>" -> "<tag>".

    CI (see .github/workflows/ci-cd.yml) always tags images with the git
    SHA that built them, so the tag IS the commit to correlate against —
    no separate deploy-metadata store needed. Returns None for images with
    no tag (bare digest references), which GitHubClient.get_commit()
    would reject anyway, but returning None here keeps the "why is there
    no commit" reason distinguishable at this layer too.
    """
    if not image or ":" not in image:
        return None
    return image.rsplit(":", 1)[-1] or None


async def investigate(
    incident: Incident,
    prom: PrometheusClient,
    loki: LokiClient,
    k8s: KubernetesClient,
    health_probe: Any = None,
    github: GitHubClient | None = None,
) -> Evidence:
    """Collect everything we can see about this incident.

    `health_probe` is an async callable ``(deployment) -> dict`` (normally
    validation.probe_health). It is injected rather than imported so tests can
    run this whole phase without a network stack.

    `github` is optional and only ever used read-only (see github_client.py).
    Passed in the same way for the same reason: tests exercise this phase
    with no real network calls, and a missing/None github client degrades
    identically to a missing GITHUB_TOKEN — commit correlation is simply
    absent, nothing here fails.
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
    }
    # Evidence isolation between concurrent incidents. These two series are
    # NOT scoped by `app` in Prometheus (the queries sum over the whole
    # cluster), so collecting them for every incident let one incident's
    # symptoms leak into another's evidence: a notification-service delivery
    # failure would flip `notification_delivery_failing` on a simultaneous
    # citizen-service HTTP-500 incident and RCA rule 7 would then blame a
    # "downstream dependency". Each series is collected only by the service
    # that owns it.
    if app == NOTIFICATION_OWNER_APP:
        metric_tasks["notification_deliveries"] = prom.notification_deliveries()
    if app == NOTIFICATION_CALLER_APP:
        metric_tasks["notification_dispatch_failures"] = prom.notification_dispatch_failures()

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

    # ---- failing init-container logs ------------------------------------
    # A pod stuck on a failing init container never starts its app
    # container, so ordinary evidence (metrics, app logs) is silent about it
    # — this is the one place Sentinel goes and looks directly at *why* the
    # init container itself is failing (e.g. an invalid/placeholder image
    # that never even reaches ImagePullBackOff cleanly). Same failure test as
    # correlation.py's `init_container_failing` finding, kept in sync with it
    # deliberately: this collects the evidence, correlation.py explains it.
    if k8s.available:
        failing_init_containers = [
            (pod.get("name"), container.get("name"))
            for pod in evidence.pods
            for container in (pod.get("container_states") or [])
            if container.get("is_init")
            and (
                container.get("waiting_reason") in CRASHLOOP_WAITING_REASONS
                or (
                    container.get("terminated_reason") is not None
                    and container.get("terminated_reason") != "Completed"
                )
            )
            and pod.get("name")
            and container.get("name")
        ]
        if failing_init_containers:
            log_results = await asyncio.gather(
                *(
                    # `previous=True`: a crash-looping container's current
                    # attempt is typically mid-backoff with no output yet —
                    # see KubernetesClient.get_container_logs's own docstring.
                    k8s.get_container_logs(namespace, pod_name, container_name, previous=True)
                    for pod_name, container_name in failing_init_containers
                ),
                return_exceptions=True,
            )
            for (pod_name, container_name), result in zip(
                failing_init_containers, log_results, strict=True
            ):
                if isinstance(result, BaseException):
                    evidence.errors.append(
                        f"collector 'init_container_logs[{pod_name}/{container_name}]' "
                        f"failed: {str(result)[:200]}"
                    )
                    continue
                evidence.init_container_logs.append(result)

    history = evidence.replicaset_history
    if history:
        newest = history[0]
        created = newest.get("created_at")
        if created:
            evidence.latest_revision_age_seconds = max(0.0, now - created)

        # ---- GitHub commit correlation for the currently-running image --
        # Deliberately sequential, not folded into the asyncio.gather above:
        # it depends on the image tag from the ReplicaSet fetch, which is
        # itself one of the parallel tasks. One extra round trip only when
        # there is a deployment to correlate at all.
        if github is not None and github.enabled:
            images = newest.get("images") or []
            sha = _extract_sha_from_image(images[0]) if images else None
            if sha:
                try:
                    evidence.deploy_commit = await github.get_commit(sha)
                except Exception as exc:  # noqa: BLE001
                    # Same fail-soft contract as every other collector here:
                    # a GitHub outage must not affect the rest of the
                    # investigation, only be recorded as a gap.
                    evidence.errors.append(
                        f"collector 'github_commit' failed: {str(exc)[:200]}"
                    )
                if evidence.deploy_commit is None and "github_commit" not in " ".join(
                    evidence.errors
                ):
                    evidence.errors.append(
                        "github commit lookup returned no result for the running "
                        f"image tag ({(sha or 'unparseable')[:12]}) — see logs for "
                        "whether this was 'not found' vs 'not a real SHA'"
                    )

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
