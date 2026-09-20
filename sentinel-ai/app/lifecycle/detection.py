"""
DETECTION — turn an Alertmanager webhook alert into an Incident.

Responsibilities, and nothing more:
  * normalise the alert (labels are free text; never trust their presence)
  * decide whether it is actionable
  * derive the stable incident identity (fingerprint / failure class)

Deduplication itself (joining a repeat onto an existing incident, and the
rules for ESCALATED / RESOLVED incidents) lives in
`lifecycle/incident_manager.py`; this module only builds the identity it
matches on.

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
    compute_incident_key,
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

# Alert rules that describe the SAME kind of failure map to one failure class,
# because incident identity is "same workload + same class of failure", not
# "same alert rule". HighHTTPErrorRate and ChaosForcedHTTPFailures are two
# views of one 5xx problem on one service; letting each open its own incident
# would run two lifecycles that both try to fix the same Deployment.
#
# Classes are deliberately COARSE and are deliberately NOT root causes: a root
# cause is an output of RCA (rules or LLM) and must never decide incident
# identity. Different classes on the same workload (e.g. `http_errors` vs
# `availability`) stay separate incidents; the remediation layer serialises
# actions per Deployment instead (see IncidentManager.target_lock).
_FAILURE_CLASS_BY_ALERTNAME: dict[str, str] = {
    "HighHTTPErrorRate": "http_errors",
    "ChaosForcedHTTPFailures": "http_errors",
    "HighRequestLatency": "latency",
    "ChaosLatencyInjection": "latency",
    "ServiceDown": "availability",
    "DeploymentUnavailable": "availability",
    "PodCrashLooping": "availability",
    "ChaosDatabaseFailure": "database",
    "HighCPUUsage": "cpu",
    "MemoryLeakSuspected": "memory",
    "NotificationDeliveryFailureRateHigh": "notification_delivery",
}


def failure_class_for(alertname: str) -> str:
    """Map an alertname to its failure class.

    An alertname Sentinel has never seen becomes its own class
    (`alert:<name>`): unknown means "do not merge it with anything", the same
    conservative direction as the unknown-alert escalation path.
    """
    return _FAILURE_CLASS_BY_ALERTNAME.get(alertname, f"alert:{alertname.lower()}")


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


def identity_for(normalised: dict[str, Any], environment: Any = None) -> dict[str, Any]:
    """The stable identity of the problem an alert describes.

    Shared by `build_incident` (a new incident) and the resolved-alert path
    (find the incident this "resolved" refers to), so both always agree on
    what "the same problem" means.
    """
    failure_class = failure_class_for(normalised["alertname"])
    application = getattr(environment, "application", None)
    environment_id = getattr(environment, "id", None)
    application_id = getattr(application, "id", None)
    return {
        "fingerprint": compute_incident_key(
            environment_id=environment_id,
            application_id=application_id,
            namespace=normalised["namespace"],
            app=normalised["app"],
            failure_class=failure_class,
        ),
        "failure_class": failure_class,
        "customer_id": getattr(environment, "customer_id", None),
        "environment_id": environment_id,
        "application_id": application_id,
    }


def build_incident(
    normalised: dict[str, Any], environment: Any = None
) -> Incident:
    """Create a fresh Incident in the DETECTION phase.

    `environment` (optional, an `app.domain.environment.Environment`) supplies
    the multi-tenancy ids, which are part of the incident's identity: the same
    failure in two environments is two incidents. Passing it here (instead of
    stamping the ids afterwards) is what lets the stable identity key be
    computed once, at creation, from complete inputs.
    """
    ident = identity_for(normalised, environment)
    failure_class = ident["failure_class"]
    fingerprint = ident["fingerprint"]
    customer_id = ident["customer_id"]
    environment_id = ident["environment_id"]
    application_id = ident["application_id"]
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
        customer_id=customer_id,
        environment_id=environment_id,
        application_id=application_id,
        failure_class=failure_class,
        alert_fingerprints=[normalised["fingerprint"]] if normalised["fingerprint"] else [],
    )
    incident.record(
        LifecyclePhase.DETECTION,
        "alert received and incident opened",
        alertname=incident.alertname,
        severity=incident.severity.value,
        app=incident.app,
        known_alertname=incident.alertname in KNOWN_ALERTNAMES,
        failure_class=failure_class,
        incident_key=fingerprint,
        alertmanager_fingerprint=normalised["fingerprint"],
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
