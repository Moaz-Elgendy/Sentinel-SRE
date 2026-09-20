"""
Sentinel Live — "what is Sentinel doing RIGHT NOW", as distinct from a
single incident's own detail page (see routers/incidents.py). One endpoint:

  GET /api/activity/status

Real state only, derived from the actual store and the actual connector
clients — never fabricated. There is deliberately no SSE endpoint of its
own here: this page's real-time updates reuse the EXISTING `/api/events`
stream (see routers/events.py, core/events.py) exactly per the "reuse the
existing SSE implementation, do not introduce a second real-time channel"
constraint — `Orchestrator._persist` already publishes an `incident_updated`
event after every real lifecycle phase transition, now enriched (see that
function) with the actual timeline message just recorded, so the frontend's
live activity feed is built entirely from genuine backend events plus this
status endpoint for the initial paint and the idle/monitoring state.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.deps import get_current_admin
from app.models.incident import IncidentStatus

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/activity", tags=["activity"])

_TERMINAL_STATUSES = {
    IncidentStatus.RESOLVED.value,
    IncidentStatus.ESCALATED.value,
    IncidentStatus.AUTO_RESOLVED.value,
}

# Short TTL cache for the connectivity probes below. Several open GUI tabs
# polling `/api/activity/status` (this endpoint has no push equivalent for
# the idle-state watcher grid — only incident activity rides the SSE bus)
# should not turn into a connectivity check per client per poll.
_watcher_cache: dict[str, Any] = {"at": 0.0, "watchers": None}
_WATCHER_CACHE_TTL_SECONDS = 5.0


async def _ping_alertmanager(url: str) -> bool:
    """Alertmanager is push-primary here (it calls Sentinel's webhook, not
    the other way around — see routers/alerts.py), so unlike Prometheus/Loki
    this is not "are we able to query it", just "is it up", for the
    Watching panel's connectivity indicator."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{url.rstrip('/')}/-/healthy")
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def _watchers(ctx: Any) -> list[dict[str, Any]]:
    now = time.time()
    if _watcher_cache["watchers"] is not None and now - _watcher_cache["at"] < _WATCHER_CACHE_TTL_SECONDS:
        return _watcher_cache["watchers"]

    prom_connected, loki_connected, am_connected = False, False, False
    try:
        prom_connected = await ctx.prom.ping()
    except Exception:  # noqa: BLE001 - a connectivity probe must never 500 this page
        pass
    try:
        loki_connected = await ctx.loki.ping()
    except Exception:  # noqa: BLE001
        pass
    try:
        am_connected = await _ping_alertmanager(ctx.settings.alertmanager_url)
    except Exception:  # noqa: BLE001
        pass

    watchers = [
        {"name": "Prometheus", "connected": prom_connected},
        {"name": "Loki", "connected": loki_connected},
        {"name": "Kubernetes", "connected": bool(ctx.k8s.available)},
        {"name": "Alertmanager", "connected": am_connected},
    ]
    _watcher_cache["watchers"] = watchers
    _watcher_cache["at"] = now
    return watchers


def _find_active_incidents(store: Any) -> list[dict[str, Any]]:
    """EVERY non-terminal incident, newest-updated first. The GUI used to be
    told about only one, which made two simultaneous incidents look like one."""
    candidates = [
        incident
        for incident in store.list_incidents(limit=50)
        if incident.get("status") not in _TERMINAL_STATUSES
    ]
    candidates.sort(key=lambda i: i.get("updated_at", 0), reverse=True)
    return candidates


def _reasoner_status(ctx: Any) -> dict[str, Any]:
    """Provider health as a CONDITION (never an incident): see
    reasoning/health.py."""
    reasoner = getattr(ctx, "reasoner", None)
    if reasoner is None:
        return {"status": "not_configured"}
    snap = reasoner.health.snapshot()
    snap["provider"] = reasoner.label
    return snap


def _find_active_incident(store: Any) -> dict[str, Any] | None:
    """The newest non-terminal incident, if any. `list_incidents` is
    already ordered newest-created-first; a handful of incidents is the
    realistic scale here (this is an SRE control room, not a ticketing
    system), so scanning that page in Python rather than adding a new
    indexed query is the appropriately simple choice."""
    candidates = [
        incident
        for incident in store.list_incidents(limit=20)
        if incident.get("status") not in _TERMINAL_STATUSES
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda i: i.get("updated_at", 0), reverse=True)
    return candidates[0]


@router.get("/status", dependencies=[Depends(get_current_admin)])
async def get_activity_status(request: Request) -> dict[str, Any]:
    ctx = getattr(request.app.state, "context", None)
    store = getattr(request.app.state, "store", None)
    if ctx is None or store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sentinel is not fully started yet",
        )

    active_all = _find_active_incidents(store)
    active = active_all[0] if active_all else None
    reasoner_status = _reasoner_status(ctx)
    if active is not None:
        timeline = active.get("timeline") or []
        last_entry = timeline[-1] if timeline else None
        return {
            "state": "investigating",
            "incident_id": active.get("id"),
            "alertname": active.get("alertname"),
            "severity": active.get("severity"),
            "status": active.get("status"),
            "phase": last_entry.get("phase") if last_entry else None,
            "message": last_entry.get("message") if last_entry else "Investigating",
            "since": active.get("created_at"),
            "updated_at": active.get("updated_at"),
            # Recent history backfills the live feed on page load; further
            # updates arrive over the existing `/api/events` SSE stream.
            "recent_timeline": timeline[-20:],
            # Additive: every in-flight incident, so concurrent incidents are
            # visible as separate entries. The single-incident keys above are
            # unchanged for existing clients.
            "active_incidents": [
                {
                    "incident_id": i.get("id"),
                    "alertname": i.get("alertname"),
                    "app": i.get("app"),
                    "severity": i.get("severity"),
                    "status": i.get("status"),
                    "phase": (i.get("timeline") or [{}])[-1].get("phase"),
                    "message": (i.get("timeline") or [{}])[-1].get("message"),
                    "updated_at": i.get("updated_at"),
                }
                for i in active_all
            ],
            "reasoner": reasoner_status,
        }

    incidents = store.list_incidents(limit=1)
    last_activity = incidents[0].get("updated_at") if incidents else None

    watchers = await _watchers(ctx)
    degraded = [w["name"] for w in watchers if not w["connected"]]

    return {
        "state": "degraded" if degraded else "monitoring",
        "message": (
            f"{', '.join(degraded)} connection unavailable"
            if degraded
            else "Waiting for an incident"
        ),
        "watchers": watchers,
        "last_activity": last_activity,
        "active_incidents": [],
        "reasoner": reasoner_status,
    }
