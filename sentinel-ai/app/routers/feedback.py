"""
SRE feedback on Sentinel's diagnosis and remediation (GUI spec sections 5
and 6).

  POST /api/incidents/{incident_id}/feedback/diagnosis
  POST /api/incidents/{incident_id}/feedback/remediation
  GET  /api/incidents/{incident_id}/feedback

This is a data-collection foundation for FUTURE evaluation/improvement of
Sentinel — see app/store/sqlite_store.py's `incident_feedback` table
docstring. Nothing here changes Sentinel's behavior: no orchestrator,
policy, or decision-engine code path reads this table. Saying so explicitly
matters because the GUI spec is explicit that the model must not be
described as retraining itself from this feedback.

`actual_root_cause`/`suggested_action` are typed as the REAL `RootCause`/
`RemediationAction` enums (app/models/incident.py) — not a hand-duplicated
list of strings — so a submission with a value Sentinel itself could never
produce is rejected by FastAPI's request validation before it reaches the
store.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.core.deps import get_current_admin
from app.models.incident import RemediationAction, RootCause

router = APIRouter(
    prefix="/api/incidents",
    tags=["feedback"],
    dependencies=[Depends(get_current_admin)],
)


class DiagnosisFeedbackRequest(BaseModel):
    correct: bool
    actual_root_cause: RootCause | None = None
    note: str | None = None


class RemediationFeedbackRequest(BaseModel):
    useful: bool
    suggested_action: RemediationAction | None = None
    note: str | None = None


def _require_incident(request: Request, incident_id: str) -> dict[str, Any]:
    store = request.app.state.store
    incident = store.get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="incident not found")
    return incident


@router.post("/{incident_id}/feedback/diagnosis", status_code=status.HTTP_201_CREATED)
def submit_diagnosis_feedback(
    incident_id: str,
    payload: DiagnosisFeedbackRequest,
    request: Request,
    current_admin: dict = Depends(get_current_admin),
) -> dict[str, Any]:
    _require_incident(request, incident_id)

    if not payload.correct and payload.actual_root_cause is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="actual_root_cause is required when correct is false",
        )

    store = request.app.state.store
    feedback_id = f"fb-{uuid.uuid4().hex[:12]}"
    store.create_feedback(
        feedback_id=feedback_id,
        incident_id=incident_id,
        kind="diagnosis",
        correct_or_useful=payload.correct,
        corrected_value=payload.actual_root_cause.value if payload.actual_root_cause else None,
        note=payload.note,
        admin_id=current_admin["id"],
    )
    return {"id": feedback_id, "incident_id": incident_id, "kind": "diagnosis"}


@router.post("/{incident_id}/feedback/remediation", status_code=status.HTTP_201_CREATED)
def submit_remediation_feedback(
    incident_id: str,
    payload: RemediationFeedbackRequest,
    request: Request,
    current_admin: dict = Depends(get_current_admin),
) -> dict[str, Any]:
    _require_incident(request, incident_id)

    if not payload.useful and payload.suggested_action is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="suggested_action is required when useful is false",
        )

    store = request.app.state.store
    feedback_id = f"fb-{uuid.uuid4().hex[:12]}"
    store.create_feedback(
        feedback_id=feedback_id,
        incident_id=incident_id,
        kind="remediation",
        correct_or_useful=payload.useful,
        corrected_value=payload.suggested_action.value if payload.suggested_action else None,
        note=payload.note,
        admin_id=current_admin["id"],
    )
    return {"id": feedback_id, "incident_id": incident_id, "kind": "remediation"}


@router.get("/{incident_id}/feedback")
def list_incident_feedback(incident_id: str, request: Request) -> dict[str, Any]:
    _require_incident(request, incident_id)
    store = request.app.state.store
    rows = store.list_feedback_for_incident(incident_id)
    for row in rows:
        row["correct_or_useful"] = bool(row["correct_or_useful"])
    return {"incident_id": incident_id, "feedback": rows}
