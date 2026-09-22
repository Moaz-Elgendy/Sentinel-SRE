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

### Sentinel Agent Evaluation additions

These distinguish failure MODES that `remediation_success_rate` alone
conflates, and surface two rates that were not computed anywhere before:

* `first_action_success_rate` — validation-passed rate of only the FIRST
  executed attempt per incident, distinct from `remediation_success_rate`
  (computed across every executed attempt, including fallbacks). A high
  overall rate with a much lower first-action rate means Sentinel's
  fallback ladder is doing the real work, not its first, best-evidenced
  decision.
* `execution_failure_rate` — of executed attempts, how many failed to even
  APPLY (`RemediationResult.succeeded is False`; a Kubernetes API error, for
  example) — distinct from an attempt that applied fine but did not fix the
  problem.
* `ineffective_remediation_rate` — of attempts that DID apply
  (`succeeded is True`) AND had a validation outcome recorded, how many
  nonetheless failed validation. This is "ran, but didn't help", the
  specific failure mode `execution_failure_rate` does not cover.
* `recovery_validation_success_rate` — passed vs. (passed+failed+timeout+
  degraded) across every attempt that had a validation outcome recorded at
  all. `ValidationOutcome.UNAVAILABLE` (no health surface configured, or
  Kubernetes unreachable) is excluded from both sides — it means "we could
  not check", not "we checked and it failed", so counting it either way
  would misrepresent Sentinel's own visibility as a remediation failure.
* `policy_rejection_rate` — of every candidate action the Policy Engine
  ever ruled on for any incident (`AttemptRecord.verdict is not None`,
  executed or denied alike), how many were denied.
* `escalation_rate` — `escalations` (an existing count) expressed as a
  fraction of `total_incidents`.
* `avg_investigation_latency_seconds` — average time from an incident's
  DETECTION timeline event to its first ROOT_CAUSE_ANALYSIS event. This IS
  independently measurable, unlike `avg_time_to_detection_seconds` above:
  both are Sentinel's own timeline events, not a comparison against
  Alertmanager's clock.

All five rates use the same `null`-when-the-denominator-is-empty rule as
every other rate in this file — a metric with zero qualifying attempts
reports `null`, never a misleading `0.0` or `1.0`.
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
    first_executed_attempts: list[dict[str, Any]] = []
    verdicted_attempts: list[dict[str, Any]] = []
    time_to_first_remediation: list[float] = []
    resolution_times: list[float] = []
    investigation_latencies: list[float] = []

    for incident in incidents:
        attempts = incident.get("attempts") or []
        created_at = incident.get("created_at")
        first_executed_at = None
        first_executed_attempt = None
        for attempt in attempts:
            if attempt.get("verdict"):
                verdicted_attempts.append(attempt)
            if attempt.get("result"):
                executed_attempts.append(attempt)
                if first_executed_at is None:
                    first_executed_at = attempt.get("at")
                    first_executed_attempt = attempt
        if first_executed_attempt is not None:
            first_executed_attempts.append(first_executed_attempt)
        if created_at and first_executed_at:
            time_to_first_remediation.append(first_executed_at - created_at)

        resolved_at = incident.get("resolved_at")
        if created_at and resolved_at and incident.get("status") in terminal:
            resolution_times.append(resolved_at - created_at)

        detection_at = None
        rca_at = None
        for event in incident.get("timeline") or []:
            if event.get("phase") == "detection" and detection_at is None:
                detection_at = event.get("at")
            if event.get("phase") == "root_cause_analysis" and rca_at is None:
                rca_at = event.get("at")
        if detection_at is not None and rca_at is not None:
            investigation_latencies.append(rca_at - detection_at)

    def _validation_outcome(attempt: dict[str, Any]) -> str | None:
        return (attempt.get("validation") or {}).get("outcome")

    validated = [a for a in executed_attempts if _validation_outcome(a) == "passed"]
    remediation_success_rate = (
        len(validated) / len(executed_attempts) if executed_attempts else None
    )

    first_action_validated = [
        a for a in first_executed_attempts if _validation_outcome(a) == "passed"
    ]
    first_action_success_rate = (
        len(first_action_validated) / len(first_executed_attempts)
        if first_executed_attempts
        else None
    )

    execution_failures = [
        a for a in executed_attempts if not (a.get("result") or {}).get("succeeded")
    ]
    execution_failure_rate = (
        len(execution_failures) / len(executed_attempts) if executed_attempts else None
    )

    applied_attempts = [a for a in executed_attempts if (a.get("result") or {}).get("succeeded")]
    applied_and_scored = [a for a in applied_attempts if _validation_outcome(a) not in (None, "unavailable")]
    ineffective = [a for a in applied_and_scored if _validation_outcome(a) != "passed"]
    ineffective_remediation_rate = (
        len(ineffective) / len(applied_and_scored) if applied_and_scored else None
    )

    validation_scored = [
        a for a in executed_attempts if _validation_outcome(a) not in (None, "unavailable")
    ]
    recovery_validation_success_rate = (
        sum(1 for a in validation_scored if _validation_outcome(a) == "passed")
        / len(validation_scored)
        if validation_scored
        else None
    )

    denied_attempts = [a for a in verdicted_attempts if not (a.get("verdict") or {}).get("allowed")]
    policy_rejection_rate = (
        len(denied_attempts) / len(verdicted_attempts) if verdicted_attempts else None
    )

    escalation_rate = len(escalated) / total if total else None

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
            "first_action_attempts": len(first_executed_attempts),
            "verdicted_attempts": len(verdicted_attempts),
            "validation_scored_attempts": len(validation_scored),
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
        "avg_investigation_latency_seconds": _mean(investigation_latencies),
        "first_action_success_rate": first_action_success_rate,
        "execution_failure_rate": execution_failure_rate,
        "ineffective_remediation_rate": ineffective_remediation_rate,
        "recovery_validation_success_rate": recovery_validation_success_rate,
        "policy_rejection_rate": policy_rejection_rate,
        "escalation_rate": escalation_rate,
    }
    if diagnosis_accuracy is None:
        result["diagnosis_feedback_unavailable_reason"] = "no diagnosis feedback recorded yet"
    if first_action_success_rate is None:
        result["first_action_success_rate_unavailable_reason"] = "no executed remediation attempts yet"
    if execution_failure_rate is None:
        result["execution_failure_rate_unavailable_reason"] = "no executed remediation attempts yet"
    if ineffective_remediation_rate is None:
        result["ineffective_remediation_rate_unavailable_reason"] = (
            "no attempt has both applied successfully and recorded a validation outcome yet"
        )
    if recovery_validation_success_rate is None:
        result["recovery_validation_success_rate_unavailable_reason"] = (
            "no attempt has recorded a validation outcome yet"
        )
    if policy_rejection_rate is None:
        result["policy_rejection_rate_unavailable_reason"] = "no candidate action has been ruled on yet"
    if escalation_rate is None:
        result["escalation_rate_unavailable_reason"] = "no incidents recorded yet"
    return result
