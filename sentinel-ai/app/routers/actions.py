"""
Sentinel action history for the Sentinel SRE Control Center GUI.

  GET /api/actions

Flattens every incident's `attempts` (see app/models/incident.py:
AttemptRecord — one `plan`/`verdict`/`result`/`validation` per remediate-
validate cycle) plus each incident's escalation, if any, into one
chronological, filterable feed. This is a read-only VIEW over data that
already exists in `incidents.body` — no new table, no new write path. It
does not use `store.record_outcome`'s `action_outcomes` table either,
because that table only tracks root-cause/action aggregates for the
Decision Engine's learning step (see learning.py) and is missing per-attempt
detail (rationale, verdict, evidence-adjacent params) this page needs.

`authorization_type` is always "autonomous" today. The one other value this
field will ever take — "temporary_sre_authorization" — is introduced by the
Phase D (temporary authorization) work per the approved plan; this endpoint
already has the field so that frontend work doesn't need a shape change
later.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from app.core.deps import get_current_admin

router = APIRouter(
    prefix="/api/actions", tags=["actions"], dependencies=[Depends(get_current_admin)]
)


def _flatten(incident: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    hypothesis = incident.get("hypothesis") or {}

    for attempt in incident.get("attempts") or []:
        plan = attempt.get("plan") or {}
        verdict = attempt.get("verdict") or {}
        result = attempt.get("result") or {}
        validation = attempt.get("validation") or {}
        if not result:
            # Policy denied the candidate before it ever executed — not an
            # "action taken", so it does not belong in the action-history
            # feed (it is still visible on the incident detail page's
            # decision/policy panel).
            continue
        rows.append(
            {
                "at": attempt.get("at"),
                "incident_id": incident.get("id"),
                "environment_id": incident.get("environment_id"),
                "application_id": incident.get("application_id"),
                "app": incident.get("app"),
                "action": result.get("action") or plan.get("action"),
                "target": (plan.get("params") or {}).get("deployment")
                or (plan.get("params") or {}).get("service"),
                "reason": plan.get("rationale") or verdict.get("detail"),
                "root_cause": hypothesis.get("root_cause"),
                "confidence": plan.get("confidence"),
                "authorization_type": "autonomous",
                "dry_run": result.get("dry_run", False),
                "succeeded": result.get("succeeded"),
                "validation_outcome": validation.get("outcome"),
                "duration_seconds": result.get("duration_seconds"),
            }
        )

    if incident.get("escalated"):
        # Escalation is itself a Sentinel action (RemediationAction.ESCALATE
        # is a real enum member — see models/incident.py) even though it
        # never produces an AttemptRecord.result, since it is a no-op
        # against the cluster.
        last_event = (incident.get("timeline") or [])[-1] if incident.get("timeline") else None
        rows.append(
            {
                "at": last_event.get("at") if last_event else incident.get("updated_at"),
                "incident_id": incident.get("id"),
                "environment_id": incident.get("environment_id"),
                "application_id": incident.get("application_id"),
                "app": incident.get("app"),
                "action": "escalate",
                "target": None,
                "reason": incident.get("escalation_detail"),
                "root_cause": hypothesis.get("root_cause"),
                "confidence": hypothesis.get("confidence"),
                "authorization_type": "autonomous",
                "dry_run": False,
                "succeeded": None,
                "validation_outcome": None,
                "duration_seconds": None,
            }
        )
    return rows


@router.get("")
def list_actions(
    request: Request,
    action: str | None = Query(default=None),
    app: str | None = Query(default=None),
    incident_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    store = request.app.state.store
    incidents = store.list_all_incidents_for_analytics()

    rows: list[dict[str, Any]] = []
    for incident in incidents:
        rows.extend(_flatten(incident))

    if action:
        rows = [r for r in rows if r["action"] == action]
    if app:
        rows = [r for r in rows if r["app"] == app]
    if incident_id:
        rows = [r for r in rows if r["incident_id"] == incident_id]

    rows.sort(key=lambda r: r.get("at") or 0, reverse=True)
    page = rows[offset : offset + limit]
    return {"count": len(rows), "limit": limit, "offset": offset, "actions": page}
