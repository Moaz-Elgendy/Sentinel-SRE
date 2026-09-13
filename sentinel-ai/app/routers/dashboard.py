"""
Dashboard summary for the Sentinel SRE Control Center GUI.

  GET /api/dashboard/summary

One aggregate, read-only call so the GUI's home page does not stitch
together five requests (and five different loading states) on every load.
Everything returned here is derived from data Sentinel already has —
`request.app.state.context` (the live `SentinelContext`, same object
`routers/environments.py` rebuilds on registration), `request.app.state.
environment`, and the incident store. Nothing is invented: a service with
no Kubernetes visibility reports `healthy: null` with a `detail` explaining
why, never a guessed status.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.deps import get_current_admin
from app.models.incident import IncidentStatus

router = APIRouter(
    prefix="/api/dashboard", tags=["dashboard"], dependencies=[Depends(get_current_admin)]
)


def _require_context(request: Request) -> Any:
    ctx = getattr(request.app.state, "context", None)
    store = getattr(request.app.state, "store", None)
    if ctx is None or store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sentinel is not fully started yet",
        )
    return ctx


async def _service_health(ctx: Any) -> list[dict[str, Any]]:
    """Per-deployment health for the dashboard's service matrix.

    Covers the application's own deployments (the allow-listed, remediable
    ones) plus the frozen-deny-listed stateful workloads (Postgres) shown
    for visibility only — Sentinel never remediates those, and this endpoint
    does not imply otherwise; see app/lifecycle/policy.py.
    """
    namespace = ctx.environment.kubernetes.namespace
    deployments = list(ctx.environment.application.deployments)
    for stateful in ctx.settings.denied_deployments_frozen:
        if stateful not in deployments:
            deployments.append(stateful)

    if not ctx.k8s.available:
        return [
            {
                "name": name,
                "healthy": None,
                "remediable": name in ctx.environment.application.deployments,
                "detail": "Kubernetes is unreachable; status unknown",
            }
            for name in deployments
        ]

    results: list[dict[str, Any]] = []
    for name in deployments:
        dep = await ctx.k8s.get_deployment(namespace, name)
        if dep is None:
            results.append(
                {
                    "name": name,
                    "healthy": None,
                    "remediable": name in ctx.environment.application.deployments,
                    "detail": "deployment not found in cluster",
                }
            )
            continue
        desired = dep.get("desired_replicas") or 0
        ready = dep.get("ready_replicas") or 0
        available = dep.get("available_replicas") or 0
        healthy = bool(desired > 0 and ready >= desired and available > 0)
        results.append(
            {
                "name": name,
                "healthy": healthy,
                "remediable": name in ctx.environment.application.deployments,
                "detail": f"{ready}/{desired} ready, {available} available",
            }
        )
    return results


def _incident_counts(store: Any) -> dict[str, Any]:
    incidents = store.list_all_incidents_for_analytics()
    terminal = {
        IncidentStatus.RESOLVED.value,
        IncidentStatus.ESCALATED.value,
        IncidentStatus.AUTO_RESOLVED.value,
    }
    active = [i for i in incidents if i.get("status") not in terminal]
    escalated = [i for i in incidents if i.get("escalated")]
    autonomous_resolved = [
        i
        for i in incidents
        if i.get("status") in (IncidentStatus.RESOLVED.value, IncidentStatus.AUTO_RESOLVED.value)
        and not i.get("escalated")
    ]
    last_incident_at = max((i.get("created_at") or 0 for i in incidents), default=None)
    recent = sorted(incidents, key=lambda i: i.get("created_at") or 0, reverse=True)[:5]
    recent_trimmed = [
        {k: v for k, v in i.items() if k not in ("evidence", "documentation", "timeline")}
        for i in recent
    ]
    return {
        "total_incidents": len(incidents),
        "active_incidents": len(active),
        "escalated_incidents": len(escalated),
        "autonomous_resolutions": len(autonomous_resolved),
        "last_incident_at": last_incident_at,
        "recent_incidents": recent_trimmed,
    }


@router.get("/summary")
async def dashboard_summary(request: Request) -> dict[str, Any]:
    ctx = _require_context(request)
    store = request.app.state.store

    services = await _service_health(ctx)
    all_healthy = all(s["healthy"] is True for s in services if s["healthy"] is not None)
    any_unknown = any(s["healthy"] is None for s in services)

    return {
        "generated_at": time.time(),
        "sentinel": {
            "monitoring": True,
            "mode": "dry_run" if ctx.settings.dry_run else "autonomous",
            "llm": "enabled" if ctx.settings.llm_enabled else "rule_based_only",
            "kubernetes_available": ctx.k8s.available,
        },
        "environment": {
            "id": ctx.environment.id,
            "name": ctx.environment.name,
            "customer_id": ctx.environment.customer_id,
        },
        "system_health": {
            "status": "unknown" if any_unknown else ("operational" if all_healthy else "degraded"),
            "services": services,
        },
        "incidents": _incident_counts(store),
    }
