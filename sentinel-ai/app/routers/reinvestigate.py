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


@router.post("/{incident_id}/suggest-fix", status_code=status.HTTP_202_ACCEPTED)
async def suggest_fix(
    incident_id: str,
    request: Request,
    current_admin: dict = Depends(get_current_admin),
) -> dict[str, Any]:
    """An operator explicitly asks Sentinel's Deep LLM Investigation for a
    bounded, typed remediation suggestion — right now, on THIS incident,
    regardless of whether it is escalated. This is the "Suggest Fix" GUI
    button (see orchestrator.suggest_fix's own docstring for the full
    trust-boundary discussion): it runs the exact same bounded read-only
    tool-use loop, typed-DSL construction and PolicyEngine gate as the
    automatic trigger, produces a proposal that still requires a SEPARATE
    human authorization (POST .../deep-proposals/{id}/authorize) before
    anything executes, and shares that trigger's per-incident budget — it
    is not a way to run Deep Investigation more often than Sentinel's own
    settings allow, only a way to run it without waiting for an escalation.

    Unlike `/reinvestigate`, this does NOT require `status == "escalated"`:
    an incident only needs evidence and a hypothesis (i.e. it has been
    investigated at least once) and a target deployment. It still takes
    the same single-writer lease as every other lifecycle action, so it
    cannot run concurrently with, or race, an active lifecycle, an
    automatic Deep Investigation, or another Suggest Fix request.
    """
    manager = get_incident_manager(request)
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sentinel is not fully started yet",
        )
    record = request.app.state.store.get_incident(incident_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="incident not found")

    incident = Incident.from_dict(record)
    if incident.evidence is None or incident.hypothesis is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "this incident has no evidence/hypothesis yet (it has not completed an "
                "investigation); Suggest Fix has nothing to reason about until it has"
            ),
        )
    if not incident.target_deployment:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this incident has no target deployment; Suggest Fix cannot propose "
            "a change with nothing to change",
        )

    actor = str(current_admin.get("username") or current_admin.get("id"))
    if not manager.start_suggest_fix(incident, actor):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a lifecycle or Deep Investigation is already running for this incident",
        )
    logger.info(
        "suggest_fix_requested",
        extra={"incident_id": incident_id, "requested_by": actor},
    )
    return {
        "incident_id": incident_id,
        "requested_by": actor,
        "detail": "Deep Investigation started in the background; poll GET "
        "/api/incidents/{id} for progress and the resulting proposal "
        "(field deep_proposals) or trace (field deep_investigation_traces)",
    }
