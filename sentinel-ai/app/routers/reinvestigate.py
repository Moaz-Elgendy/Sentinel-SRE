"""
POST /api/incidents/{id}/reinvestigate — an administrator asks Sentinel to
look at an ESCALATED incident again.

This is the "explicit manual re-run" way out of ESCALATED (the other human way
is a temporary authorization, routers/authorizations.py). It does not touch
the cluster and does not bypass anything: it reopens the incident and runs the
ordinary lifecycle — RCA -> decision -> Policy Engine -> remediation — so every
safety check applies exactly as for an automatic run.

Unlike an automatic reopen it does not require the evidence to have changed
(a person asking is itself the reason) and does not spend the automatic reopen
budget (`max_incident_reopens`). It still cannot create a second lifecycle: it
takes the same single-writer lease as everything else, and a re-run against an
incident that is not currently ESCALATED is refused.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.deps import get_current_admin
from app.models.incident import Incident
from app.routers.alerts import get_incident_manager

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/incidents",
    tags=["incidents"],
    dependencies=[Depends(get_current_admin)],
)


@router.post("/{incident_id}/reinvestigate", status_code=status.HTTP_202_ACCEPTED)
async def reinvestigate(
    incident_id: str,
    request: Request,
    current_admin: dict = Depends(get_current_admin),
) -> dict[str, Any]:
    manager = get_incident_manager(request)
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sentinel is not fully started yet",
        )
    record = request.app.state.store.get_incident(incident_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="incident not found")
    if record.get("status") != "escalated":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"incident status is '{record.get('status')}', not 'escalated'; only an "
                "escalated incident can be re-investigated"
            ),
        )
    actor = str(current_admin.get("username") or current_admin.get("id"))
    incident = Incident.from_dict(record)
    if not manager.start_manual_reinvestigation(incident, actor):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a lifecycle is already running for this incident",
        )
    logger.info(
        "manual_reinvestigation_requested",
        extra={"reinvestigated_incident": incident_id, "requested_by": actor},
    )
    return {
        "incident_id": incident_id,
        "requested_by": actor,
        "detail": "re-investigation started through the normal lifecycle and Policy Engine; "
        "poll GET /api/incidents/{id} for progress",
    }
