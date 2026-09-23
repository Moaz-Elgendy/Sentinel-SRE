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
from app.models.incident import ActionParams, DeepProposalStatus, Incident, RemediationAction

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

# "An authorization is executing for this incident" is the IncidentManager's
# single-writer lease (lifecycle/incident_manager.py), the same lease a running
# lifecycle holds. One shared lease is what stops a double-click — or an
# authorization racing an automatic re-check — from putting two writers on one
# incident record. Process-local, matching this codebase's single-process
# design (see app/core/events.py's own note on that same limitation).


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
                              authorization_id: str, params: ActionParams | None, incident_id: str,
                              manager: Any) -> None:
    """Background wrapper. Always releases the incident lease."""
    try:
        await orchestrator.authorize_and_remediate(incident, action, authorization_id, params)
    finally:
        if manager is not None:
            manager.release(incident_id)


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

    from app.routers.alerts import get_incident_manager  # noqa: PLC0415

    manager = get_incident_manager(request)
    if manager is not None and manager.is_running(incident_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a lifecycle or authorized remediation is already running for this incident",
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

    incident = Incident.from_dict(incident_dict)
    if manager is not None and not manager.try_acquire(incident):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a lifecycle or authorized remediation is already running for this incident",
        )

    authorization_id = f"authz-{uuid.uuid4().hex[:12]}"
    now = time.time()
    try:
        store.create_temporary_authorization(
            authorization_id=authorization_id,
            incident_id=incident_id,
            action=payload.action.value,
            params_json=json.dumps(params.to_dict() if params else {}),
            granted_by=current_admin["id"],
            granted_at=now,
            expires_at=now + AUTHORIZATION_TTL_SECONDS,
        )
    except Exception:
        # Never leave the lease held if the run was never scheduled: the
        # incident would look "running" forever.
        if manager is not None:
            manager.release(incident_id)
        raise
    logger.info(
        "temporary_authorization_granted",
        extra={
            "incident_id": incident_id,
            "action": payload.action.value,
            "authorization_id": authorization_id,
            "granted_by": current_admin["id"],
        },
    )

    background.add_task(
        _run_authorization, orchestrator, incident, payload.action, authorization_id, params,
        incident_id, manager,
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


# ---------------------------------------------------------------------------
# Deep Investigation proposals — human authorization of a novel typed action
# (lifecycle/deep_investigation.py, models/incident.py's NovelActionType).
#
#   GET  /api/incidents/{incident_id}/deep-proposals
#   POST /api/incidents/{incident_id}/deep-proposals/{proposal_id}/authorize
#
# Deliberately the SAME shape as the known-action authorize endpoint above,
# reusing the same `temporary_authorizations` table (no schema change: the
# `action` column stores "deep_remediation:<proposal_id>", unique per
# proposal so it can never collide with a known action's own authorization
# row) and the same IncidentManager single-writer lease. The differences are
# exactly the differences between the two proposal shapes: there is no
# `action` field to pick from a menu (the proposal already names its own
# action_type/target — that is the whole point of "structured, not
# free-form"), and there is no `human_override`-style confidence bypass:
# `PolicyEngine.evaluate_deep_proposal` has its own confidence floor with no
# override parameter at all (see that method's docstring).
# ---------------------------------------------------------------------------
AUTHORIZE_DEEP_ACTION_PREFIX = "deep_remediation:"


async def _run_deep_authorization(
    orchestrator: Any, incident: Incident, proposal_id: str, authorization_id: str,
    incident_id: str, manager: Any,
) -> None:
    """Background wrapper. Always releases the incident lease — identical
    pattern to `_run_authorization` above."""
    try:
        await orchestrator.authorize_and_remediate_deep(incident, proposal_id, authorization_id)
    finally:
        if manager is not None:
            manager.release(incident_id)


@router.get("/{incident_id}/deep-proposals")
def list_deep_proposals(incident_id: str, request: Request) -> dict[str, Any]:
    incident_dict = request.app.state.store.get_incident(incident_id)
    if incident_dict is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="incident not found")
    return {
        "incident_id": incident_id,
        "deep_proposals": incident_dict.get("deep_proposals") or [],
    }


@router.post(
    "/{incident_id}/deep-proposals/{proposal_id}/authorize",
    status_code=status.HTTP_202_ACCEPTED,
)
def authorize_deep_proposal(
    incident_id: str,
    proposal_id: str,
    request: Request,
    background: BackgroundTasks,
    current_admin: dict = Depends(get_current_admin),
) -> dict[str, Any]:
    # Deliberately NOT `_require_escalated_incident`: a Deep Investigation
    # proposal can now come from the explicit Suggest Fix flow
    # (orchestrator.suggest_fix), which by design works on an incident that
    # is not, and may never become, ESCALATED — see suggest_fix's own
    # docstring. Authorization eligibility is carried entirely by the
    # proposal's own `status == "suggested"` check below, exactly like the
    # rest of this endpoint already worked; only the incident-status
    # precondition is being relaxed here.
    incident_dict = request.app.state.store.get_incident(incident_id)
    if incident_dict is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="incident not found")

    proposal = next(
        (p for p in (incident_dict.get("deep_proposals") or []) if p.get("id") == proposal_id),
        None,
    )
    if proposal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no deep proposal {proposal_id!r} on this incident",
        )
    if proposal.get("status") != "suggested":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"deep proposal {proposal_id} has status {proposal.get('status')!r}, not "
                "'suggested'; it has already been authorised, executed, or rejected"
            ),
        )

    from app.routers.alerts import get_incident_manager  # noqa: PLC0415

    manager = get_incident_manager(request)
    if manager is not None and manager.is_running(incident_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a lifecycle or authorized remediation is already running for this incident",
        )

    orchestrator = getattr(request.app.state, "orchestrator", None)
    store = request.app.state.store
    if orchestrator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Sentinel is not fully started yet"
        )

    incident = Incident.from_dict(incident_dict)
    if manager is not None and not manager.try_acquire(incident):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a lifecycle or authorized remediation is already running for this incident",
        )

    authorization_id = f"authz-{uuid.uuid4().hex[:12]}"
    now = time.time()
    try:
        store.create_temporary_authorization(
            authorization_id=authorization_id,
            incident_id=incident_id,
            action=f"{AUTHORIZE_DEEP_ACTION_PREFIX}{proposal_id}",
            params_json=json.dumps(proposal.get("target") or {}),
            granted_by=current_admin["id"],
            granted_at=now,
            expires_at=now + AUTHORIZATION_TTL_SECONDS,
        )
    except Exception:
        if manager is not None:
            manager.release(incident_id)
        raise
    logger.info(
        "deep_proposal_authorization_granted",
        extra={
            "incident_id": incident_id,
            "proposal_id": proposal_id,
            "action_type": proposal.get("action_type"),
            "authorization_id": authorization_id,
            "granted_by": current_admin["id"],
        },
    )

    background.add_task(
        _run_deep_authorization, orchestrator, incident, proposal_id, authorization_id,
        incident_id, manager,
    )

    return {
        "authorization_id": authorization_id,
        "incident_id": incident_id,
        "proposal_id": proposal_id,
        "action_type": proposal.get("action_type"),
        "scope": "this proposal only",
        "permanent_policy_changed": False,
        "expires_at": now + AUTHORIZATION_TTL_SECONDS,
        "detail": "processing in the background; poll GET /api/incidents/{id} for progress",
    }


@router.post("/{incident_id}/deep-proposals/{proposal_id}/reject")
def reject_deep_proposal(
    incident_id: str,
    proposal_id: str,
    request: Request,
    current_admin: dict = Depends(get_current_admin),
) -> dict[str, Any]:
    """A human explicitly declines a suggested Deep Investigation proposal.

    Unlike `/authorize`, this never touches the Kubernetes client, the
    Policy Engine, or the Remediation Engine — it only records a status
    change and a timeline entry, synchronously, with no incident lease and
    no background task, because there is nothing here that mutates the
    cluster. The one thing it does guard against is racing a concurrent
    lifecycle/Suggest Fix run that could overwrite this exact write with a
    stale copy of the incident record; a proposal can always still be
    rejected once that finishes.
    """
    from app.routers.alerts import get_incident_manager  # noqa: PLC0415

    manager = get_incident_manager(request)
    if manager is not None and manager.is_running(incident_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "a lifecycle or Deep Investigation is currently running for this "
                "incident; try again once it finishes"
            ),
        )

    store = request.app.state.store
    incident_dict = store.get_incident(incident_id)
    if incident_dict is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="incident not found")

    incident = Incident.from_dict(incident_dict)
    proposal = next((p for p in incident.deep_proposals if p.id == proposal_id), None)
    if proposal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no deep proposal {proposal_id!r} on this incident",
        )
    if proposal.status != DeepProposalStatus.SUGGESTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"deep proposal {proposal_id} has status {proposal.status.value!r}, not "
                "'suggested'; it has already been authorised, executed, or rejected"
            ),
        )

    actor = str(current_admin.get("username") or current_admin.get("id"))
    proposal.status = DeepProposalStatus.REJECTED
    proposal.rejected_reason = f"rejected by {actor}"
    incident.record(
        incident.phase,
        f"Deep Investigation proposal {proposal_id} "
        f"({proposal.action_type.value}) rejected by {actor}.",
        proposal_id=proposal_id,
        action_type=proposal.action_type.value,
    )
    store.upsert_incident(incident.to_dict())
    logger.info(
        "deep_proposal_rejected",
        extra={"incident_id": incident_id, "proposal_id": proposal_id, "rejected_by": actor},
    )
    return {
        "incident_id": incident_id,
        "proposal_id": proposal_id,
        "status": DeepProposalStatus.REJECTED.value,
        "rejected_by": actor,
    }
