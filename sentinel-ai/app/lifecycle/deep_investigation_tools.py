"""
Read-only investigation tools for the bounded Deep Investigation loop
(see lifecycle/deep_investigation.py's module docstring for the loop itself).

Every tool here wraps an EXISTING read-only client method
(KubernetesClient/PrometheusClient/LokiClient) that investigation.py already
uses to build the initial evidence bundle — this module adds no new way to
reach the cluster, Prometheus or Loki, only a way to reach the SAME
narrow surface on demand, one call at a time, so the model can follow its own
hypothesis instead of only ever seeing one fixed snapshot.

### Why the model can never fabricate a tool result

The loop in deep_investigation.py only ever folds a tool result into the next
turn's prompt AFTER this module's `call_tool()` has actually run the
corresponding client call and returned a real `ToolResult`. There is no code
path where a turn's own claim about what a tool "found" is trusted instead of
calling it — the orchestration loop, not the model, decides what text
representing a tool result exists at all. A model can still LIE in its
prose ("the logs clearly show X") but it cannot make Sentinel act as though
tool X returned data it never did, because nothing downstream of this module
reads the model's narration as evidence; only `call_tool()`'s own return
value is.

### Why every tool is scoped to the incident, hard

No tool here takes a namespace, deployment, or arbitrary target as a
parameter — `ToolContext` pins `namespace`/`deployment` from the incident
once, at loop start, and every handler reads them from there. A tool call
can widen WHAT it looks at within that fixed target (which pod, which
container, which metric, how far back) but never WHERE — there is no
parameter shape that lets a turn ask about a different namespace or a
different Deployment. `get_container_logs` goes one step further: it only
accepts a pod/container this SAME investigation has already observed via
`inspect_pods`/`inspect_deployment` (tracked in `ToolContext.known_pods` /
`known_containers`), so a model cannot even try to read logs from something
it was never shown.

### Bounding

Every tool's result is truncated to `ToolContext.tool_output_max_chars`
before it is ever returned to the caller (deep_investigation.py folds the
SAME truncated text into the next prompt — there is no larger version kept
anywhere for the model to eventually see). Per-tool bounds (log tail lines,
event window, Loki result limit) are clamped inside each handler,
independent of whatever a turn's parameters ask for — the same
"never trust the caller's number" posture `KubernetesClient.get_container_logs`
already applies to `tail_lines`.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.clients.kubernetes_client import KubernetesClient, find_previous_revision
from app.clients.loki import LokiClient, summarise
from app.clients.prometheus import PrometheusClient
from app.models.incident import Incident

logger = logging.getLogger(__name__)

# metric name (as offered to the model) -> PrometheusClient method name.
# Deliberately the SAME set investigation.py's own collectors gather (see
# that module's LOOKBACK_SECONDS comment, which names these exact tool
# concepts as the future adaptive-investigator surface this module is) — a
# metric the initial evidence bundle cannot see is not offered here either.
_METRIC_HANDLERS: dict[str, str] = {
    "error_rate": "error_rate",
    "error_count": "error_count",
    "request_rate": "request_rate",
    "p95_latency_seconds": "p95_latency",
    "cpu_cores": "cpu_cores",
    "memory_bytes": "memory_bytes",
    "memory_growth_bytes": "memory_growth_bytes",
    "up": "up",
    # Evidence-isolation guard mirrors investigation.py's own comment: these
    # two series are not scoped by `app` in Prometheus, so each is only
    # offered for the ONE service that owns that half of the signal.
    "notification_deliveries": "notification_deliveries",
    "notification_dispatch_failures": "notification_dispatch_failures",
}
_NOTIFICATION_OWNER_APP = "notification-service"
_NOTIFICATION_CALLER_APP = "citizen-service"

LOOKBACK_SECONDS = 900  # matches investigation.py's own window


@dataclass
class ToolResult:
    """What a tool call actually produced. `ok=False` means the call was
    refused or failed (bad params, unreachable dependency, unknown target) —
    still a real, recorded result, just a negative one; never an exception
    escaping to the loop."""

    ok: bool
    data: Any = None
    error: str | None = None


@dataclass
class ToolContext:
    """Everything a tool handler needs, pinned once at investigation start.

    `known_pods`/`known_containers` start empty and are populated by
    `inspect_pods`/`inspect_deployment` as the investigation runs — this is
    the mechanism that keeps `get_container_logs` from accepting a
    pod/container the model merely asserts exists.
    """

    incident: Incident
    k8s: KubernetesClient
    prom: PrometheusClient | None = None
    loki: LokiClient | None = None
    health_probe: Callable[[str], Awaitable[dict[str, Any]]] | None = None
    tool_output_max_chars: int = 3000
    known_pods: set[str] = field(default_factory=set)
    known_containers: set[str] = field(default_factory=set)

    @property
    def namespace(self) -> str:
        return self.incident.namespace

    @property
    def deployment(self) -> str | None:
        return self.incident.target_deployment


# ---------------------------------------------------------------------------
# Handlers — each is `async def handler(ctx, params: dict) -> ToolResult`,
# never raises (call_tool() below still wraps every call in a try/except as
# a second line of defence).
# ---------------------------------------------------------------------------
async def _inspect_pods(ctx: ToolContext, params: dict[str, Any]) -> ToolResult:
    if not ctx.deployment:
        return ToolResult(ok=False, error="incident has no target deployment")
    if not ctx.k8s.available:
        return ToolResult(ok=False, error="Kubernetes API is not available")
    pods = await ctx.k8s.list_pods(ctx.namespace, label_selector=f"app={ctx.deployment}")
    for pod in pods:
        name = pod.get("name")
        if name:
            ctx.known_pods.add(name)
        for cs in pod.get("container_states") or []:
            cname = cs.get("name")
            if cname:
                ctx.known_containers.add(cname)
    return ToolResult(ok=True, data=pods)


async def _inspect_deployment(ctx: ToolContext, params: dict[str, Any]) -> ToolResult:
    if not ctx.deployment:
        return ToolResult(ok=False, error="incident has no target deployment")
    if not ctx.k8s.available:
        return ToolResult(ok=False, error="Kubernetes API is not available")
    dep = await ctx.k8s.get_deployment(ctx.namespace, ctx.deployment)
    if dep is None:
        return ToolResult(ok=False, error="deployment not found")
    for container in dep.get("containers") or []:
        cname = container.get("name")
        if cname:
            ctx.known_containers.add(cname)
    return ToolResult(ok=True, data=dep)


async def _inspect_replicasets(ctx: ToolContext, params: dict[str, Any]) -> ToolResult:
    if not ctx.deployment:
        return ToolResult(ok=False, error="incident has no target deployment")
    if not ctx.k8s.available:
        return ToolResult(ok=False, error="Kubernetes API is not available")
    replicasets = await ctx.k8s.list_replicasets(ctx.namespace, ctx.deployment)
    # Strip `_template` (a raw kubernetes client object, not JSON-serialisable
    # and not something the model needs — it reasons about images/revisions/
    # images_valid, never reconstructs a template itself).
    cleaned = [{k: v for k, v in rs.items() if k != "_template"} for rs in replicasets]
    return ToolResult(ok=True, data=cleaned)


async def _inspect_events(ctx: ToolContext, params: dict[str, Any]) -> ToolResult:
    if not ctx.deployment:
        return ToolResult(ok=False, error="incident has no target deployment")
    if not ctx.k8s.available:
        return ToolResult(ok=False, error="Kubernetes API is not available")
    try:
        since_seconds = float(params.get("since_seconds", LOOKBACK_SECONDS))
    except (TypeError, ValueError):
        since_seconds = float(LOOKBACK_SECONDS)
    since_seconds = max(60.0, min(since_seconds, 3600.0))
    events = await ctx.k8s.list_events(ctx.namespace, since_seconds=since_seconds, limit=50)
    return ToolResult(ok=True, data=events)


async def _get_container_logs(ctx: ToolContext, params: dict[str, Any]) -> ToolResult:
    pod = params.get("pod")
    container = params.get("container")
    if not isinstance(pod, str) or pod not in ctx.known_pods:
        return ToolResult(
            ok=False,
            error=(
                "unknown pod: call inspect_pods first and name exactly one of the "
                "pod names it returned"
            ),
        )
    if not isinstance(container, str) or (
        ctx.known_containers and container not in ctx.known_containers
    ):
        return ToolResult(
            ok=False,
            error=(
                "unknown container: call inspect_pods or inspect_deployment first "
                "and name exactly one of the container names they returned"
            ),
        )
    previous = bool(params.get("previous", False))
    try:
        tail_lines = int(params.get("tail_lines", 100))
    except (TypeError, ValueError):
        tail_lines = 100
    tail_lines = max(1, min(tail_lines, KubernetesClient.MAX_LOG_TAIL_LINES))
    result = await ctx.k8s.get_container_logs(
        ctx.namespace, pod, container, tail_lines=tail_lines, previous=previous
    )
    return ToolResult(ok=True, data=result)


async def _query_metric(ctx: ToolContext, params: dict[str, Any]) -> ToolResult:
    if ctx.prom is None:
        return ToolResult(ok=False, error="Prometheus is not configured")
    metric = params.get("metric")
    if metric not in _METRIC_HANDLERS:
        return ToolResult(
            ok=False, error=f"unknown metric; allowed: {sorted(_METRIC_HANDLERS)}"
        )
    if metric == "notification_deliveries" and ctx.deployment != _NOTIFICATION_OWNER_APP:
        return ToolResult(
            ok=False,
            error="notification_deliveries is only meaningful for notification-service",
        )
    if metric == "notification_dispatch_failures" and ctx.deployment != _NOTIFICATION_CALLER_APP:
        return ToolResult(
            ok=False,
            error="notification_dispatch_failures is only meaningful for citizen-service",
        )
    method = getattr(ctx.prom, _METRIC_HANDLERS[metric])
    if metric in ("notification_deliveries", "notification_dispatch_failures"):
        value = await method()
    else:
        value = await method(ctx.deployment)
    return ToolResult(ok=True, data={"metric": metric, "value": value})


async def _query_logs(ctx: ToolContext, params: dict[str, Any]) -> ToolResult:
    if ctx.loki is None:
        return ToolResult(ok=False, error="Loki is not configured")
    kind = params.get("kind", "errors")
    if kind not in ("errors", "access_5xx", "all"):
        return ToolResult(ok=False, error="kind must be one of: errors, access_5xx, all")
    try:
        limit = int(params.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 100))
    now = time.time()
    start = now - LOOKBACK_SECONDS
    if kind == "errors":
        entries = await ctx.loki.recent_errors(ctx.deployment, ctx.namespace, start, now, limit=limit)
    elif kind == "access_5xx":
        entries = await ctx.loki.access_log_errors(
            ctx.deployment, ctx.namespace, start, now, limit=limit
        )
    else:
        entries = await ctx.loki.recent_lines(ctx.deployment, ctx.namespace, start, now, limit=limit)
    return ToolResult(
        ok=True,
        data={"kind": kind, "count": len(entries), "sample": summarise(entries, max_samples=10)},
    )


async def _inspect_previous_revision(ctx: ToolContext, params: dict[str, Any]) -> ToolResult:
    """Surfaces the SAME validated rollback-candidate logic
    `RemediationEngine._rollback`/`find_previous_revision` actually use at
    execution time — not a re-derived approximation — so the model reasons
    about rollback viability with the real answer to "is there a safe target"
    rather than assuming the numerically-previous revision is healthy."""
    if not ctx.deployment:
        return ToolResult(ok=False, error="incident has no target deployment")
    if not ctx.k8s.available:
        return ToolResult(ok=False, error="Kubernetes API is not available")
    replicasets = await ctx.k8s.list_replicasets(ctx.namespace, ctx.deployment)
    target_revision = params.get("target_revision")
    try:
        target_revision = int(target_revision) if target_revision is not None else None
    except (TypeError, ValueError):
        target_revision = None
    candidate = find_previous_revision(replicasets, target_revision)
    if candidate is None:
        return ToolResult(
            ok=True,
            data={
                "valid_rollback_target": False,
                "reason": (
                    "no older revision exists whose container AND init-container "
                    "images are all valid (or the requested target_revision does "
                    "not exist) — do not assume an older revision is automatically "
                    "healthy"
                ),
            },
        )
    return ToolResult(
        ok=True,
        data={
            "valid_rollback_target": True,
            "revision": candidate.get("revision"),
            "images": candidate.get("images"),
            "init_images": candidate.get("init_images"),
            "created_at": candidate.get("created_at"),
        },
    )


async def _inspect_service_health(ctx: ToolContext, params: dict[str, Any]) -> ToolResult:
    if ctx.health_probe is None or not ctx.deployment:
        return ToolResult(ok=False, error="no health probe available for this target")
    health = await ctx.health_probe(ctx.deployment)
    return ToolResult(ok=True, data=health)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
TOOL_SPECS: dict[str, dict[str, Any]] = {
    "inspect_pods": {
        "handler": _inspect_pods,
        "params": "none",
        "description": "List this incident's pods: phase, readiness, restart count, container/init-container states.",
    },
    "inspect_deployment": {
        "handler": _inspect_deployment,
        "params": "none",
        "description": "Fetch the Deployment's current spec: containers, images, replicas, redacted env vars.",
    },
    "inspect_replicasets": {
        "handler": _inspect_replicasets,
        "params": "none",
        "description": "List revision history (newest first): revision number, images, images_valid, ready_replicas.",
    },
    "inspect_events": {
        "handler": _inspect_events,
        "params": '{"since_seconds": optional int 60-3600, default 900}',
        "description": "Recent Kubernetes events for this namespace (ScalingReplicaSet, Killing, Unhealthy, BackOff, ...).",
    },
    "get_container_logs": {
        "handler": _get_container_logs,
        "params": (
            '{"pod": required string (must be a pod name from inspect_pods), '
            '"container": required string (must be a container from inspect_pods/'
            'inspect_deployment), "previous": optional bool, "tail_lines": optional int up to 200}'
        ),
        "description": "Bounded tail of one named container's logs on one named pod.",
    },
    "query_metric": {
        "handler": _query_metric,
        "params": f'{{"metric": required, one of {sorted(_METRIC_HANDLERS)}}}',
        "description": "Read one Prometheus metric for this incident's own app — never a raw PromQL string.",
    },
    "query_logs": {
        "handler": _query_logs,
        "params": '{"kind": one of "errors"|"access_5xx"|"all", "limit": optional int up to 100}',
        "description": "Bounded, deduplicated recent Loki log sample for this incident's own app/namespace.",
    },
    "inspect_previous_revision": {
        "handler": _inspect_previous_revision,
        "params": '{"target_revision": optional int}',
        "description": "The SAME validated rollback-candidate check the Remediation Engine uses: is there a safe revision to roll back to, and if not, why not.",
    },
    "inspect_service_health": {
        "handler": _inspect_service_health,
        "params": "none",
        "description": "Probe this incident's service health endpoint (/healthz or /readyz) right now.",
    },
}


def describe_evidence_sources_for_prompt() -> str:
    lines = []
        for name in TOOL_SPECS:
            lines.append(f'- "{name}"')
        return "\n".join(lines)


async def call_tool(ctx: ToolContext, name: Any, params: Any) -> ToolResult:
    """Look up and run exactly one tool. Never raises — a tool that blows up
    is recorded as a failed, bounded result, not an investigation-ending
    crash."""
    if not isinstance(name, str) or name not in TOOL_SPECS:
        return ToolResult(ok=False, error=f"unknown tool {name!r}; see the allowed tool list")
    if not isinstance(params, dict):
        params = {}
    try:
        return await TOOL_SPECS[name]["handler"](ctx, params)
    except Exception as exc:  # noqa: BLE001 - a tool must never crash the loop
        logger.warning(
            "deep_investigation_tool_failed",
            extra={"tool": name, "error_detail": str(exc)[:200]},
        )
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {str(exc)[:200]}")


def summarize_result(result: ToolResult, max_chars: int) -> str:
    """Bounded JSON text of a tool result, for folding into the next turn's
    prompt AND for `ToolCallRecord.result_summary` (the audit trail) — the
    same text is used for both, so the audit record always shows exactly
    what the model was actually given, never a different, prettier summary."""
    payload = {"ok": result.ok, "error": result.error, "data": result.data} if not result.ok else {
        "ok": True,
        "data": result.data,
    }
    text = json.dumps(payload, default=str)
    if len(text) > max_chars:
        text = text[: max(0, max_chars - 15)] + "...<truncated>"
    return text
