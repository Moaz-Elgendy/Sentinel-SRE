"""
POST /api/alerts/webhook — the Alertmanager receiver.

Two properties this endpoint must have:

1. **It returns promptly.** Alertmanager has a short HTTP timeout and retries
   on a slow response. A full lifecycle run takes minutes (settle period plus
   validation polling), so nothing is processed inline: the handler asks the
   IncidentManager what to do with each alert and returns immediately.

2. **Correlation is decided by the IncidentManager, not here.** This module
   only translates Alertmanager's payload into decisions and a response. The
   rules (stable incident identity, joining a running incident, the escalated
   incident that must not re-escalate, recurrence after resolution) live in
   `app/lifecycle/incident_manager.py`.

   Each NEW incident runs as its own asyncio task, so alerts in one payload
   (or arriving together) progress concurrently and independently. They used
   to be queued as FastAPI BackgroundTasks, which Starlette runs one after
   another: the second alert in a payload waited for the first incident's
   whole lifecycle.

No authentication on this endpoint. It is a ClusterIP Service reachable only
from inside the cluster, matching how the app services' /metrics endpoints are
exposed. If Sentinel were ever exposed through an Ingress this would need a
shared secret, since the payload chooses which service gets remediated.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

from app.lifecycle import detection
from app.lifecycle.incident_manager import IncidentManager
from app.models.incident import AlertmanagerWebhook

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


def get_incident_manager(request: Request) -> IncidentManager | None:
    """The app-wide IncidentManager, bound to the CURRENT orchestrator.

    Rebinding on every call keeps it correct when an environment registration
    swaps `app.state.orchestrator` (routers/environments.py) without that
    router needing to know the manager exists.
    """
    state = request.app.state
    orchestrator = getattr(state, "orchestrator", None)
    store = getattr(state, "store", None)
    if orchestrator is None or store is None:
        return None
    manager = getattr(state, "incident_manager", None)
    if manager is None:
        manager = IncidentManager(
            store,
            getattr(state, "settings", None),
            event_bus=getattr(state, "event_bus", None),
            orchestrator=orchestrator,
        )
        state.incident_manager = manager
    manager.orchestrator = orchestrator
    return manager


@router.post("/webhook")
async def alertmanager_webhook(
    payload: AlertmanagerWebhook,
    request: Request,
) -> dict[str, Any]:
    """Accept an Alertmanager v4 payload and hand each alert to the manager."""
    manager = get_incident_manager(request)

    if manager is None:
        # Startup has not finished. 200 with an explanatory body rather than
        # 503: a 503 makes Alertmanager retry, and a retry storm during
        # Sentinel's own startup is not useful.
        logger.warning("webhook_received_before_startup_complete")
        return {"accepted": 0, "detail": "Sentinel is still starting up"}

    # Phase 1: exactly one environment is registered, so this is simply "the"
    # environment for this Sentinel process (main.py:lifespan sets it). See
    # Environment's docstring for why routing an inbound webhook to ONE of
    # several environments is deliberately not built yet.
    environment = getattr(request.app.state, "environment", None)

    accepted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for alert in payload.alerts:
        normalised = detection.normalise_alert(alert)

        # ---- resolved alerts -------------------------------------------
        if normalised["status"] == "resolved":
            skipped.append(manager.handle_resolved(normalised, environment))
            continue

        actionable, reason = detection.is_actionable(normalised)
        if not actionable:
            logger.info(
                "alert_not_actionable",
                extra={"alertname": normalised["alertname"], "skip_reason": reason},
            )
            skipped.append({"alertname": normalised["alertname"], "reason": reason})
            continue

        decision = manager.handle_alert(normalised, environment)
        if decision.created:
            incident = decision.incident
            accepted.append(
                {
                    "incident_id": incident.id,
                    "alertname": incident.alertname,
                    "app": incident.app,
                    "severity": incident.severity.value,
                    "occurrence": incident.occurrence,
                }
            )
        else:
            skipped.append(
                {
                    "alertname": normalised["alertname"],
                    "incident_id": decision.incident_id,
                    "correlation": decision.kind.value,
                    "reason": decision.reason,
                }
            )

    return {
        "accepted": len(accepted),
        "incidents": accepted,
        "skipped": skipped,
        # Stated in the response so a curl-based test makes the async
        # behaviour obvious rather than looking like nothing happened.
        "detail": "lifecycle runs are processed in the background; poll "
        "GET /api/incidents/{id} for progress",
    }
