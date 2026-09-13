"""
Sentinel performance/learning stats for the Sentinel SRE Control Center GUI.

  GET /api/performance/summary

Every number here is computed from `incidents.body` (already the audited
record — see orchestrator.py's `_persist`) and, since Phase C, from the
`incident_feedback` table (see routers/feedback.py) at request time. Nothing
is fabricated or hard-coded, and where a requested metric genuinely cannot
be derived honestly, the field is returned as `null` with an
`unavailable_reason` rather than a made-up value. Two fields still fall in
that category:

* `temporary_overrides` needs the temporary-authorization mechanism, which
  is Phase D and touches app/lifecycle/policy.py — not built yet.
* `avg_time_to_detection_seconds` is left `null` with an explanation rather
  than a misleading number: Sentinel is alerted by Alertmanager (push, not
  poll), so its own DETECTION timeline event fires essentially the instant
  the webhook is received — there is no independent "time to detect" for
  Sentinel to measure. What Alertmanager took to notice and fire the alert
  is a property of the alerting rules, not of Sentinel, and is not in this
  service's data at all.

`diagnosis_accuracy`/`incorrect_diagnoses` use, per incident, only the MOST
RECENT diagnosis feedback row — feedback is append-only (an SRE can submit
more than once as an incident is re-investigated or a judgment is revised),
and counting every historical round would let one incident's back-and-forth
outweigh a single clean judgment on another. This is purely a reporting
aggregate: nothing here feeds back into Sentinel's own diagnosis behavior —
see feedback.py's module docstring.

`remediation_success_rate` and `avg_time_to_remediation_seconds` are
computed across executed attempts (`AttemptRecord.result is not None`), not
denied candidates.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.core.deps import get_current_admin
from app.models.incident import IncidentStatus

router = APIRouter(
    prefix="/api/performance", tags=["performance"], dependencies=[Depends(get_current_admin)]
)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _latest_feedback_per_incident(feedback_rows: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in feedback_rows:
        if row.get("kind") != kind:
            continue
        incident_id = row["incident_id"]
        existing = latest.get(incident_id)
        if existing is None or row["created_at"] > existing["created_at"]:
            latest[incident_id] = row
    return list(latest.values())


@router.get("/summary")
def performance_summary(request: Request) -> dict[str, Any]:
    store = request.app.state.store
    incidents = store.list_all_incidents_for_analytics()
    feedback_rows = store.list_all_feedback_for_analytics()

    terminal = {
        IncidentStatus.RESOLVED.value,
        IncidentStatus.ESCALATED.value,
        IncidentStatus.AUTO_RESOLVED.value,
    }
    resolved_like = {IncidentStatus.RESOLVED.value, IncidentStatus.AUTO_RESOLVED.value}

    total = len(incidents)
    escalated = [i for i in incidents if i.get("escalated")]
    autonomous_resolutions = [
        i for i in incidents if i.get("status") in resolved_like and not i.get("escalated")
    ]

    executed_attempts: list[dict[str, Any]] = []
    time_to_first_remediation: list[float] = []
    resolution_times: list[float] = []

    for incident in incidents:
        attempts = incident.get("attempts") or []
        created_at = incident.get("created_at")
        first_executed_at = None
        for attempt in attempts:
            if attempt.get("result"):
                executed_attempts.append(attempt)
                if first_executed_at is None:
                    first_executed_at = attempt.get("at")
        if created_at and first_executed_at:
            time_to_first_remediation.append(first_executed_at - created_at)

        resolved_at = incident.get("resolved_at")
        if created_at and resolved_at and incident.get("status") in terminal:
            resolution_times.append(resolved_at - created_at)

    validated = [
        a for a in executed_attempts if (a.get("validation") or {}).get("outcome") == "passed"
    ]
    remediation_success_rate = (
        len(validated) / len(executed_attempts) if executed_attempts else None
    )

    diagnosis_judgments = _latest_feedback_per_incident(feedback_rows, "diagnosis")
    diagnosis_accuracy = (
        sum(1 for row in diagnosis_judgments if row["correct_or_useful"]) / len(diagnosis_judgments)
        if diagnosis_judgments
        else None
    )
    incorrect_diagnoses = (
        sum(1 for row in diagnosis_judgments if not row["correct_or_useful"])
        if diagnosis_judgments
        else None
    )

    remediation_judgments = _latest_feedback_per_incident(feedback_rows, "remediation")
    remediation_feedback_useful_rate = (
        sum(1 for row in remediation_judgments if row["correct_or_useful"]) / len(remediation_judgments)
        if remediation_judgments
        else None
    )

    result: dict[str, Any] = {
        "sample_size": {
            "total_incidents": total,
            "executed_attempts": len(executed_attempts),
            "diagnosis_feedback_count": len(diagnosis_judgments),
            "remediation_feedback_count": len(remediation_judgments),
        },
        "diagnosis_accuracy": diagnosis_accuracy,
        "incorrect_diagnoses": incorrect_diagnoses,
        "remediation_success_rate": remediation_success_rate,
        "remediation_feedback_useful_rate": remediation_feedback_useful_rate,
        "autonomous_resolutions": len(autonomous_resolutions),
        "escalations": len(escalated),
        "temporary_overrides": None,
        "temporary_overrides_unavailable_reason": (
            "temporary SRE authorization is a later implementation phase"
        ),
        "avg_incident_resolution_seconds": _mean(resolution_times),
        "avg_time_to_detection_seconds": None,
        "avg_time_to_detection_unavailable_reason": (
            "Sentinel is alerted by Alertmanager (push-based); it has no independent "
            "measurement of detection latency to report"
        ),
        "avg_time_to_remediation_seconds": _mean(time_to_first_remediation),
    }
    if diagnosis_accuracy is None:
        result["diagnosis_feedback_unavailable_reason"] = "no diagnosis feedback recorded yet"
    return result
