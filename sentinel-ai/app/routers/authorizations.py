"""
Temporary SRE authorization for an escalated incident (GUI spec section 7 /
approved plan Phase D).

  POST /api/incidents/{incident_id}/authorize
  GET  /api/incidents/{incident_id}/authorizations

This is the ONE feature in the Sentinel GUI that can cause a cluster
mutation from a human's click rather than Sentinel's own decision — so it
gets the most scrutiny of anything in this codebase's GUI layer. Read this
alongside lifecycle/policy.py's `human_override` parameter docstring and
lifecycle/orchestrator.py's `authorize_and_remediate` docstring before
touching either.

What this endpoint does NOT do:
  - It does not call the Kubernetes client, the Policy Engine, or the
    Remediation Engine directly. It creates one audit row (a
    `TemporaryAuthorization`) and hands off to the orchestrator's own
    `authorize_and_remediate`, which is the only thing that touches policy
    and remediation.
  - It does not change `PolicyConfig`'s thresholds. The grant is scoped to
    exactly (incident_id, action), single-use, and expires in
    `AUTHORIZATION_TTL_SECONDS` — see `temporary_authorizations` in
    app/store/sqlite_store.py.
  - It does not skip any policy check other than the confidence number —
    see `human_override`'s docstring for what that means precisely.

What "authorized" requires:
  - The incident must exist and be CURRENTLY escalated (`status ==
    "escalated"`) — not merely have been escalated at some point in its
    history. A resolved or still-autonomously-in-progress incident cannot
    be authorized.
  - `action` must be one of the four real remediation actions (never
    ESCALATE — escalating needs no authorization, it is always permitted).
    It is deliberately NOT restricted to "whatever Sentinel's hypothesis
    happened to recommend": many escalations happen because no candidate
    existed at all (hypothesis.recommended_action is itself ESCALATE), and
    the GUI spec's own mockup offers the SRE several buttons to choose from
    (restart / rollback / scale). Safety here comes from the Policy Engine
    still gating whichever action is chosen, not from restricting the menu.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.core.deps import get_current_admin
from app.models.incident import ActionParams, Incident, RemediationAction

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/incidents",
    tags=["authorizations"],
    dependencies=[Depends(get_current_admin)],
)

# How long a grant is usable before it expires unused. Short on purpose: this
# is a one-time exception for the incident in front of the SRE right now,
# not a standing credential — see the module docstring.
AUTHORIZATION_TTL_SECONDS = 15 * 60

# Incident ids with an authorization currently executing. Mirrors
# routers/alerts.py's `_in_flight` fingerprint set — prevents a double-click
# from starting two concurrent remediation attempts against the same
# incident. Process-local, matching this codebase's single-process design
# (see app/core/events.py's own note on that same limitation).
_in_flight: set[str] = set()


class AuthorizeRequest(BaseModel):
    action: RemediationAction
    namespace: str | None = None
    deployment: str | None = None
    replicas: int | None = None
    target_revision: int | None = None
    service: str | None = None


def _require_escalated_incident(request: Request, incident_id: str) -> dict[str, Any]:
    store = request.app.state.store
    incident = store.get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="incident not found")
    if incident.get("status") != "escalated":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"incident status is '{incident.get('status')}', not 'escalated'; "
                "temporary authorization only applies to a currently-escalated incident"
            ),
        )
    return incident


async def _run_authorization(orchestrator: Any, incident: Incident, action: RemediationAction,
                              authorization_id: str, params: ActionParams | None, incident_id: str) -> None:
    """Background wrapper — mirrors routers/alerts.py's `_run_lifecycle`."""
    try:
        await orchestrator.authorize_and_remediate(incident, action, authorization_id, params)
    finally:
        _in_flight.discard(incident_id)


@router.post("/{incident_id}/authorize", status_code=status.HTTP_202_ACCEPTED)
def authorize_incident(
    incident_id: str,
    payload: AuthorizeRequest,
    request: Request,
    background: BackgroundTasks,
    current_admin: dict = Depends(get_current_admin),
) -> dict[str, Any]:
    if payload.action is RemediationAction.ESCALATE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="escalation is always permitted and needs no authorization",
        )

    incident_dict = _require_escalated_incident(request, incident_id)

    if incident_id in _in_flight:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="an authorized remediation is already running for this incident",
        )

    orchestrator = getattr(request.app.state, "orchestrator", None)
    store = request.app.state.store
    if orchestrator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Sentinel is not fully started yet"
        )

    params = None
    if any([payload.namespace, payload.deployment, payload.replicas, payload.target_revision, payload.service]):
        params = ActionParams(
            namespace=payload.namespace or incident_dict.get("namespace"),
            deployment=payload.deployment or incident_dict.get("app"),
            replicas=payload.replicas,
            target_revision=payload.target_revision,
            service=payload.service or incident_dict.get("app"),
        )

    authorization_id = f"authz-{uuid.uuid4().hex[:12]}"
    now = time.time()
    store.create_temporary_authorization(
        authorization_id=authorization_id,
        incident_id=incident_id,
        action=payload.action.value,
        params_json=json.dumps(params.to_dict() if params else {}),
        granted_by=current_admin["id"],
        granted_at=now,
        expires_at=now + AUTHORIZATION_TTL_SECONDS,
    )
    logger.info(
        "temporary_authorization_granted",
        extra={
            "incident_id": incident_id,
            "action": payload.action.value,
            "authorization_id": authorization_id,
            "granted_by": current_admin["id"],
        },
    )

    incident = Incident.from_dict(incident_dict)
    _in_flight.add(incident_id)
    background.add_task(
        _run_authorization, orchestrator, incident, payload.action, authorization_id, params, incident_id
    )

    return {
        "authorization_id": authorization_id,
        "incident_id": incident_id,
        "action": payload.action.value,
        "scope": "this incident only",
        "permanent_policy_changed": False,
        "expires_at": now + AUTHORIZATION_TTL_SECONDS,
        "detail": "processing in the background; poll GET /api/incidents/{id} for progress",
    }


@router.get("/{incident_id}/authorizations")
def list_authorizations(incident_id: str, request: Request) -> dict[str, Any]:
    _require_incident_exists(request, incident_id)
    store = request.app.state.store
    rows = store.list_temporary_authorizations_for_incident(incident_id)
    for row in rows:
        row["params"] = json.loads(row.pop("params_json"))
        row["permanent_policy_changed"] = bool(row["permanent_policy_changed"])
    return {"incident_id": incident_id, "authorizations": rows}


def _require_incident_exists(request: Request, incident_id: str) -> None:
    store = request.app.state.store
    if store.get_incident(incident_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="incident not found")
