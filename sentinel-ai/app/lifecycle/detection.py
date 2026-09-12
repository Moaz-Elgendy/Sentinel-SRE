"""
DETECTION — turn an Alertmanager webhook alert into an Incident.

Responsibilities, and nothing more:
  * normalise the alert (labels are free text; never trust their presence)
  * decide whether it is actionable
  * deduplicate against open incidents

Deduplication matters more than it looks. Alertmanager re-sends a firing
alert every `repeat_interval` (typically 4h, often much shorter in a demo
setup), and a flapping service can produce a burst. Without dedup, Sentinel
would open a fresh incident per firing and each one would independently
decide to restart the same Deployment — a restart loop driven by the
remediator itself. The per-incident action cap does not save you here,
because each duplicate incident has its own cap. So dedup IS a safety
control, not just tidiness.

Unknown alertnames are handled, not rejected. The known set is
`ServiceDown`, `HighHTTPErrorRate`, `HighRequestLatency`,
`NotificationDeliveryFailureRateHigh`, `ChaosForcedHTTPFailures`,
`ChaosDatabaseFailure`, `ChaosLatencyInjection`, plus the ones expected to
appear soon (`HighCPUUsage`, `MemoryLeakSuspected`, `PodCrashLooping`,
`DeploymentUnavailable`). Anything else still gets an incident: it goes
through investigation and RCA, and if RCA cannot form a confident hypothesis
the Policy Engine will not authorise anything and it escalates to a human.
That is the correct behaviour for a novel alert — investigate and hand over,
not crash and not guess.
"""
from __future__ import annotations

import logging
from typing import Any

from app.models.incident import (
    AlertmanagerAlert,
    Incident,
    IncidentStatus,
    LifecyclePhase,
    Severity,
    compute_fingerprint,
    new_incident_id,
)

logger = logging.getLogger(__name__)

# Alertnames Sentinel has an explicit RCA rule for. Membership here changes
# nothing about *permissions* — it only affects how confident the rule engine
# can be. Kept as a frozenset so it is obvious this is a lookup table, not
# configuration.
KNOWN_ALERTNAMES: frozenset[str] = frozenset(
    {
        "ServiceDown",
        "HighHTTPErrorRate",
        "HighRequestLatency",
        "NotificationDeliveryFailureRateHigh",
        "ChaosForcedHTTPFailures",
        "ChaosDatabaseFailure",
        "ChaosLatencyInjection",
        # Expected to be added by the alert rules; handled pre-emptively.
        "HighCPUUsage",
        "MemoryLeakSuspected",
        "PodCrashLooping",
        "DeploymentUnavailable",
    }
)

# Watchdog-style alerts that exist to prove the pipeline works. Remediating
# them would be nonsense.
IGNORED_ALERTNAMES: frozenset[str] = frozenset({"Watchdog", "DeadMansSwitch"})


class DetectionResult:
    """Either a new incident, a join onto an existing one, or a rejection."""

    def __init__(
        self,
        incident: Incident | None,
        is_new: bool,
        ignored: bool = False,
        reason: str = "",
    ) -> None:
        self.incident = incident
        self.is_new = is_new
        self.ignored = ignored
        self.reason = reason


def normalise_alert(alert: AlertmanagerAlert) -> dict[str, Any]:
    """Extract the fields we care about, with every access defensive.

    `app` is looked up under several keys because different alert rules label
    differently: our own rules use `app`, but a rule written against the
    kubernetes_pods job might carry `service` or `job` instead. `deployment`
    is checked too since an operator-written rule may use it.
    """
    labels = alert.labels or {}
    annotations = alert.annotations or {}

    app = (
        labels.get("app")
        or labels.get("deployment")
        or labels.get("service")
        or labels.get("job")
    )
    # `job` on the kubernetes_pods scrape is "kubernetes-pods", which is the
    # scrape job name and not a service. Treat it as unknown rather than
    # trying to remediate a Deployment called "kubernetes-pods".
    if app == "kubernetes-pods":
        app = None

    return {
        "alertname": labels.get("alertname") or "UnknownAlert",
        "severity": Severity.parse(labels.get("severity")),
        "app": app,
        "namespace": labels.get("kubernetes_namespace")
        or labels.get("namespace")
        or "citizen-portal",
        "pod": labels.get("kubernetes_pod_name") or labels.get("pod"),
        "summary": annotations.get("summary", "") or "",
        "description": annotations.get("description", "") or "",
        "labels": dict(labels),
        "annotations": dict(annotations),
        "fingerprint": alert.fingerprint,
        "status": (alert.status or "firing").lower(),
        "startsAt": alert.startsAt,
    }


def is_actionable(normalised: dict[str, Any]) -> tuple[bool, str]:
    """Should this alert open an incident at all?

    We accept unknown alertnames on purpose (see module docstring). The only
    rejections are watchdogs and alerts with no identifiable target service —
    the latter because every remediation action needs a Deployment name, so
    an alert we cannot attribute to a service can only ever escalate, and
    opening an incident that is born escalated adds noise without adding
    information. It is logged at WARNING so it is still visible.
    """
    alertname = normalised["alertname"]
    if alertname in IGNORED_ALERTNAMES:
        return False, f"{alertname} is a pipeline watchdog, not an incident"
    if normalised["status"] == "resolved":
        return False, "alert arrived already resolved"
    return True, ""


def build_incident(normalised: dict[str, Any]) -> Incident:
    """Create a fresh Incident in the DETECTION phase."""
    fingerprint = normalised["fingerprint"] or compute_fingerprint(
        normalised["alertname"], normalised["app"], normalised["pod"]
    )
    incident = Incident(
        id=new_incident_id(),
        fingerprint=fingerprint,
        alertname=normalised["alertname"],
        severity=normalised["severity"],
        app=normalised["app"],
        namespace=normalised["namespace"],
        pod=normalised["pod"],
        summary=normalised["summary"],
        description=normalised["description"],
        labels=normalised["labels"],
        annotations=normalised["annotations"],
        started_at_raw=normalised["startsAt"],
        status=IncidentStatus.OPEN,
    )
    incident.record(
        LifecyclePhase.DETECTION,
        "alert received and incident opened",
        alertname=incident.alertname,
        severity=incident.severity.value,
        app=incident.app,
        known_alertname=incident.alertname in KNOWN_ALERTNAMES,
        fingerprint_source="alertmanager" if normalised["fingerprint"] else "synthesised",
    )
    if incident.alertname not in KNOWN_ALERTNAMES:
        # Not an error. Recorded explicitly so the incident document is
        # honest about Sentinel operating outside its rule table.
        incident.record(
            LifecyclePhase.DETECTION,
            "alertname is not in Sentinel's known set; proceeding with generic "
            "evidence-driven RCA. If no confident hypothesis emerges this will "
            "escalate to a human rather than guess at an action.",
            alertname=incident.alertname,
        )
        logger.warning(
            "unknown_alertname", extra={"alertname": incident.alertname}
        )
    return incident
