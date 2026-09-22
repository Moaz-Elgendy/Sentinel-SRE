"""
Sentinel Live — the GUI's "what is Sentinel doing right now" panel.

  GET /api/activity/status

One read-only endpoint, same shape of contract as routers/dashboard.py: never
invent a status. If Sentinel has a real open (non-terminal) incident, report
exactly what that incident's own timeline says (phase, last message, recent
history) — nothing paraphrased. Otherwise report "monitoring" or "degraded"
depending on whether any of the four things Sentinel watches (Prometheus,
Loki, Kubernetes, Alertmanager) is actually reachable right now, the same
honest-connectivity approach as PrometheusClient.ping/LokiClient.ping (see
app/clients/prometheus.py, app/clients/loki.py) rather than assuming
"configured" means "up".

Alertmanager has no dedicated client (Sentinel is only ever pushed TO by
Alertmanager via routers/alerts.py — it never queries it), so this hits
Alertmanager's own `/-/healthy` endpoint directly, the same convention
Prometheus and Loki's clients already use.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.deps import get_current_admin
from app.models.incident import Incident, IncidentStatus

router = APIRouter(
    prefix="/api/activity", tags=["activity"], dependencies=[Depends(get_current_admin)]
)

# How many of the most recent timeline entries to surface for the
# "investigating" state — enough to show recent progress without dragging
# the whole (potentially long) history over the wire on every poll.
RECENT_TIMELINE_LIMIT = 10

# Non-terminal == still being worked, per IncidentStatus.is_terminal.
_ACTIVE_STATUSES = tuple(s.value for s in IncidentStatus if not s.is_terminal)


def _require_context(request: Request) -> Any:
    ctx = getattr(request.app.state, "context", None)
    store = getattr(request.app.state, "store", None)
    if ctx is None or store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sentinel is not fully started yet",
        )
    return ctx


def _active_incidents(store: Any) -> list[dict[str, Any]]:
    """All non-terminal incidents right now (any status that is not
    resolved/escalated/auto_resolved)."""
    return store.list_by_statuses(_ACTIVE_STATUSES)


def _find_active_incident(candidates: list[dict[str, Any]]) -> Incident | None:
    """The most recently updated of an already-fetched set of non-terminal
    incidents, if any.

    `store.list_by_statuses` returns oldest-first (it is built for startup
    recovery); this endpoint wants the newest one, so sort here rather than
    add a second, endpoint-specific ordering to the store.
    """
    if not candidates:
        return None
    latest = max(candidates, key=lambda d: d.get("updated_at") or 0.0)
    return Incident.from_dict(latest)


async def _ping_alertmanager(base_url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/-/healthy")
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def _watchers(ctx: Any) -> list[dict[str, Any]]:
    prom_ok, loki_ok, alertmanager_ok = await asyncio.gather(
        ctx.prom.ping(),
        ctx.loki.ping(),
        _ping_alertmanager(ctx.settings.alertmanager_url),
    )
    return [
        {"name": "Prometheus", "connected": prom_ok},
        {"name": "Loki", "connected": loki_ok},
        {"name": "Kubernetes", "connected": ctx.k8s.available},
        {"name": "Alertmanager", "connected": alertmanager_ok},
    ]


def _last_activity(store: Any) -> float | None:
    incidents = store.list_all_incidents_for_analytics()
    if not incidents:
        return None
    return max((i.get("updated_at") or 0.0) for i in incidents)


def _reasoner_status(ctx: Any) -> dict[str, Any]:
    """Same provider-health snapshot rca.py already tracks (see
    app/reasoning/health.py) — surfaced here so Sentinel Live can show
    REASONER_UNAVAILABLE without polling a separate endpoint. `enabled=False`
    (no provider configured — the fully-supported rules-only mode) is
    distinguished from a configured-but-failing provider.
    """
    if ctx.reasoner is None:
        return {"enabled": False, "label": None, "status": "disabled"}
    return {"enabled": True, "label": ctx.reasoner.label, **ctx.reasoner.health.snapshot()}


@router.get("/status")
async def activity_status(request: Request) -> dict[str, Any]:
    ctx = _require_context(request)
    store = request.app.state.store

    watchers = await _watchers(ctx)
    active_incidents = _active_incidents(store)
    active = _find_active_incident(active_incidents)
    reasoner = _reasoner_status(ctx)

    if active is not None:
        recent = active.timeline[-RECENT_TIMELINE_LIMIT:]
        last_message = recent[-1].message if recent else ""
        return {
            "state": "investigating",
            "incident_id": active.id,
            "alertname": active.alertname,
            "phase": active.phase.value,
            "message": last_message,
            "recent_timeline": [e.to_dict() for e in recent],
            "watchers": watchers,
            "reasoner": reasoner,
            "active_incidents": len(active_incidents),
        }

    return {
        "state": "monitoring" if any(w["connected"] for w in watchers) else "degraded",
        "watchers": watchers,
        "last_activity": _last_activity(store),
        "reasoner": reasoner,
        "active_incidents": len(active_incidents),
    }
