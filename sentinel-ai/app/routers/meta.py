"""
Static vocabulary lookups for the Sentinel SRE Control Center GUI.

Every value here is read directly off the enums in app/models/incident.py —
the single source of truth the rest of Sentinel already uses (the closed
`RemediationAction` set, the bounded `RootCause` list, the mandated
`LifecyclePhase` order). Nothing is hand-duplicated as a second list, so a
future enum change cannot silently desync the GUI's dropdowns/diagram from
what Sentinel is actually capable of.

Read-only, admin-auth-gated like every other GUI API — there is nothing
sensitive here, but there is also no reason for it to be reachable without a
session, and consistency with the rest of the GUI API surface is worth more
than the minor convenience of leaving it open.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.core.deps import get_current_admin
from app.models.incident import LifecyclePhase, RemediationAction, RootCause

router = APIRouter(prefix="/api/meta", tags=["meta"], dependencies=[Depends(get_current_admin)])

# Human-readable labels for the timeline/flow diagram. Kept here (not
# invented per-component in the frontend) so every page that renders a phase
# uses the same wording.
_PHASE_LABELS: dict[str, str] = {
    LifecyclePhase.DETECTION.value: "Alert Detected",
    LifecyclePhase.INVESTIGATION.value: "Collecting Evidence",
    LifecyclePhase.CORRELATION.value: "Correlating Signals",
    LifecyclePhase.ROOT_CAUSE_ANALYSIS.value: "Root Cause Analysis",
    LifecyclePhase.REMEDIATION_DECISION.value: "Remediation Decision",
    LifecyclePhase.POLICY_CHECK.value: "Policy Evaluation",
    LifecyclePhase.AUTONOMOUS_EXECUTION.value: "Executing Remediation",
    LifecyclePhase.RECOVERY_VALIDATION.value: "Validating Recovery",
    LifecyclePhase.RE_INVESTIGATION.value: "Re-Investigating",
    LifecyclePhase.DOCUMENTATION.value: "Documenting Incident",
    LifecyclePhase.NOTIFICATION.value: "Notifying",
    LifecyclePhase.LEARNING.value: "Recording Outcome",
    LifecyclePhase.ESCALATION.value: "Escalated to SRE",
}

# The canonical, linear "happy path" shown in the Live Sentinel Operations
# flow diagram. RE_INVESTIGATION/ESCALATION are real phases an incident can
# visit (see LifecyclePhase docstring) but are rendered as branches off this
# spine, not as additional steps in it — see the frontend's LiveFlowDiagram.
_PRIMARY_FLOW_ORDER: list[str] = [
    LifecyclePhase.DETECTION.value,
    LifecyclePhase.INVESTIGATION.value,
    LifecyclePhase.CORRELATION.value,
    LifecyclePhase.ROOT_CAUSE_ANALYSIS.value,
    LifecyclePhase.REMEDIATION_DECISION.value,
    LifecyclePhase.POLICY_CHECK.value,
    LifecyclePhase.AUTONOMOUS_EXECUTION.value,
    LifecyclePhase.RECOVERY_VALIDATION.value,
    LifecyclePhase.DOCUMENTATION.value,
]


@router.get("/root-causes")
def list_root_causes() -> dict[str, Any]:
    return {"root_causes": [rc.value for rc in RootCause]}


@router.get("/actions")
def list_actions() -> dict[str, Any]:
    return {"actions": [a.value for a in RemediationAction]}


@router.get("/lifecycle-phases")
def list_lifecycle_phases() -> dict[str, Any]:
    return {
        "phases": [p.value for p in LifecyclePhase],
        "primary_flow_order": _PRIMARY_FLOW_ORDER,
        "labels": _PHASE_LABELS,
    }
