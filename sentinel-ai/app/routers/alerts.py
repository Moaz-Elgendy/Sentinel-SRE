"""
POST /api/alerts/webhook — the Alertmanager receiver.

Two properties this endpoint must have:

1. **It returns promptly.** Alertmanager has a short HTTP timeout and retries
   on a slow response. A full lifecycle run takes minutes (settle period plus
   validation polling), so processing inline would guarantee retries, and each
   retry would look like a fresh firing. The lifecycle therefore runs in a
   FastAPI BackgroundTask and the handler returns immediately with what it
   decided to do.

2. **It is idempotent enough.** Alertmanager re-sends firing alerts on its
   `repeat_interval`, and retries on any timeout. Dedup by fingerprint means a
   repeat joins the existing open incident instead of starting a second
   lifecycle against the same service. Without that, two concurrent
   lifecycles would each restart the same Deployment — the per-incident action
   cap does not help, because each duplicate incident has its own cap.

   The dedup guard is an in-process set of fingerprints currently being
   processed, plus the SQLite lookup for incidents that are open but not
   actively running. In-process is sufficient because Sentinel runs as a
   single replica; if it were ever scaled to two, this would need a lock in
   the store and that is called out here rather than left as a surprise.

No authentication on this endpoint. It is a ClusterIP Service reachable only
from inside the cluster, matching how the app services' /metrics endpoints are
exposed. If Sentinel were ever exposed through an Ingress this would need a
shared secret, since the payload chooses which service gets remediated.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request

from app.lifecycle import detection
from app.models.incident import AlertmanagerWebhook, IncidentStatus, LifecyclePhase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

# Fingerprints with a lifecycle currently running in this process.
_in_flight: set[str] = set()


@router.post("/webhook")
async def alertmanager_webhook(
    payload: AlertmanagerWebhook,
    background: BackgroundTasks,
    request: Request,
) -> dict[str, Any]:
    """Accept an Alertmanager v4 payload and schedule lifecycle runs."""
    orchestrator = getattr(request.app.state, "orchestrator", None)
    store = getattr(request.app.state, "store", None)
    settings_obj = getattr(request.app.state, "settings", None)

    if orchestrator is None or store is None:
        # Startup has not finished. 200 with an explanatory body rather than
        # 503: a 503 makes Alertmanager retry, and a retry storm during
        # Sentinel's own startup is not useful.
        logger.warning("webhook_received_before_startup_complete")
        return {"accepted": 0, "detail": "Sentinel is still starting up"}

    accepted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for alert in payload.alerts:
        normalised = detection.normalise_alert(alert)

        # ---- resolved alerts -------------------------------------------
        if normalised["status"] == "resolved":
            result = _handle_resolved(normalised, store)
            skipped.append(result)
            continue

        actionable, reason = detection.is_actionable(normalised)
        if not actionable:
            logger.info(
                "alert_not_actionable",
                extra={"alertname": normalised["alertname"], "skip_reason": reason},
            )
            skipped.append(
                {"alertname": normalised["alertname"], "reason": reason}
            )
            continue

        # ---- dedup ------------------------------------------------------
        fingerprint = normalised["fingerprint"] or None
        incident = detection.build_incident(normalised)
        fingerprint = incident.fingerprint

        if fingerprint in _in_flight:
            skipped.append(
                {
                    "alertname": incident.alertname,
                    "reason": "a lifecycle for this fingerprint is already running; "
                    "this repeat firing was folded into it",
                }
            )
            logger.info(
                "duplicate_firing_in_flight",
                extra={"alertname": incident.alertname, "fingerprint": fingerprint},
            )
            continue

        window = getattr(settings_obj, "incident_dedup_window_seconds", 3600)
        existing = store.find_open_by_fingerprint(fingerprint, window)
        if existing is not None:
            # An open incident exists but nothing is running for it (e.g.
            # Sentinel restarted mid-incident). Bump the firing count and
            # leave it alone rather than starting a competing lifecycle. A
            # human looking at /api/incidents sees a stuck incident, which is
            # the honest signal.
            existing["firing_count"] = int(existing.get("firing_count", 1)) + 1
            existing["updated_at"] = incident.created_at
            store.upsert_incident(existing)
            skipped.append(
                {
                    "alertname": incident.alertname,
                    "incident_id": existing.get("id"),
                    "reason": "joined an existing open incident (deduplicated by "
                    "fingerprint); no second lifecycle was started",
                }
            )
            logger.info(
                "alert_deduplicated",
                extra={
                    "alertname": incident.alertname,
                    "existing_incident_id": existing.get("id"),
                    "firing_count": existing["firing_count"],
                },
            )
            continue

        # ---- schedule the lifecycle ------------------------------------
        store.upsert_incident(incident.to_dict())
        _in_flight.add(fingerprint)
        background.add_task(_run_lifecycle, orchestrator, incident, fingerprint)
        accepted.append(
            {
                "incident_id": incident.id,
                "alertname": incident.alertname,
                "app": incident.app,
                "severity": incident.severity.value,
            }
        )
        logger.info(
            "incident_opened",
            extra={
                "incident_id": incident.id,
                "alertname": incident.alertname,
                "app": incident.app,
                "severity": incident.severity.value,
            },
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


def _handle_resolved(normalised: dict[str, Any], store) -> dict[str, Any]:
    """Alertmanager says the condition cleared.

    If we have a matching open incident, mark it auto_resolved and record
    that the resolution was NOT Sentinel's doing. That distinction matters:
    counting self-healing blips as successful remediations would make
    Sentinel's effectiveness metrics — and its learning bias — a lie.
    """
    from app.models.incident import compute_fingerprint  # noqa: PLC0415

    fingerprint = normalised["fingerprint"] or compute_fingerprint(
        normalised["alertname"], normalised["app"], normalised["pod"]
    )
    existing = store.find_open_by_fingerprint(fingerprint, 24 * 3600)
    if existing is None:
        return {
            "alertname": normalised["alertname"],
            "reason": "resolved notification with no matching open incident",
        }

    existing["status"] = IncidentStatus.AUTO_RESOLVED.value
    existing["resolved_at"] = existing.get("updated_at")
    existing.setdefault("timeline", []).append(
        {
            "phase": LifecyclePhase.DETECTION.value,
            "message": "Alertmanager reported the alert as resolved. The incident "
            "is closed as auto_resolved: the condition cleared on its own or by "
            "someone else's action, NOT as a verified result of a Sentinel "
            "remediation. Counting this as a Sentinel success would corrupt both "
            "the effectiveness metrics and the learning bias.",
            "at": existing.get("updated_at"),
            "detail": {},
        }
    )
    store.upsert_incident(existing)
    logger.info(
        "incident_auto_resolved",
        extra={"incident_id": existing.get("id"), "alertname": normalised["alertname"]},
    )
    return {
        "alertname": normalised["alertname"],
        "incident_id": existing.get("id"),
        "reason": "marked auto_resolved from an Alertmanager resolved notification",
    }


async def _run_lifecycle(orchestrator, incident, fingerprint: str) -> None:
    """Background wrapper. Always clears the in-flight marker."""
    try:
        await orchestrator.run(incident)
    finally:
        _in_flight.discard(fingerprint)
