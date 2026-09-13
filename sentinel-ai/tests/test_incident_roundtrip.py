"""
Round-trip test for `Incident.from_dict()` (app/models/incident.py), added
for the Sentinel GUI's temporary-authorization feature (Phase D).

This is deliberately tested against a REAL incident produced by a real
lifecycle run (via the same `gui_client`/alert-webhook fixtures used in
test_gui_api.py) rather than a hand-built fixture — a hand-built fixture
would only prove the two functions agree with each other's assumptions
about the data's shape, not that they agree with what the orchestrator
actually produces and SQLiteStore actually stores.
"""
from __future__ import annotations

from app.models.incident import Incident


def _fire_alert(client, fingerprint="roundtrip-fp-1", app="citizen-service"):
    payload = {
        "version": "4",
        "status": "firing",
        "commonLabels": {},
        "commonAnnotations": {},
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "HighHTTPErrorRate",
                    "app": app,
                    "severity": "critical",
                },
                "annotations": {"summary": f"High 5xx rate on {app}"},
                "startsAt": "2026-09-12T07:00:00Z",
                "fingerprint": fingerprint,
            }
        ],
    }
    resp = client.post("/api/alerts/webhook", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["incidents"][0]["incident_id"]


def _wait_for_terminal(client, headers, incident_id, timeout=10.0):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/api/incidents/{incident_id}", headers=headers)
        assert resp.status_code == 200
        incident = resp.json()
        if incident["status"] in ("resolved", "escalated", "auto_resolved"):
            return incident
        time.sleep(0.2)
    raise AssertionError(f"incident {incident_id} did not reach a terminal state in time")


def test_from_dict_round_trips_a_real_escalated_incident(gui_client):
    """No Kubernetes/Prometheus/Loki reachable in this test environment, so
    this incident escalates with zero executed attempts — see
    test_gui_api.py's equivalent fixture for why that is a genuine, useful
    lifecycle path to exercise, not a degraded test."""
    client, login = gui_client
    headers = login(client)

    incident_id = _fire_alert(client)
    stored = _wait_for_terminal(client, headers, incident_id)

    reconstructed = Incident.from_dict(stored)
    assert reconstructed.to_dict() == stored

    # A few fields checked explicitly too, so a future bug that happens to
    # produce an equal-but-wrong dict (e.g. two enums whose .value strings
    # collide) doesn't hide behind the blanket equality check above.
    assert reconstructed.id == stored["id"]
    assert reconstructed.status.value == stored["status"]
    assert reconstructed.escalated == stored["escalated"]
    assert reconstructed.hypothesis is not None
    assert reconstructed.hypothesis.root_cause.value == stored["hypothesis"]["root_cause"]


def test_from_dict_round_trips_an_incident_with_a_full_attempt():
    """Directly builds an Incident with a fully-populated AttemptRecord
    (plan + verdict + result + validation) so every nested from_dict path
    gets exercised deterministically — coaxing a real successful
    remediation out of the live app isn't possible in this test environment
    (no real Kubernetes), and that is test_remediation_allowlist.py's job,
    not this file's."""
    import time

    from app.models.incident import (
        ActionParams,
        ActionPlan,
        AttemptRecord,
        Evidence,
        Hypothesis,
        Incident,
        PolicyVerdict,
        RemediationAction,
        RemediationResult,
        RootCause,
        Severity,
        ValidationOutcome,
        ValidationReport,
    )

    plan = ActionPlan(
        action=RemediationAction.RESTART_DEPLOYMENT,
        params=ActionParams(namespace="citizen-portal", deployment="citizen-service"),
        confidence=0.93,
        rationale="test rationale",
    )
    verdict = PolicyVerdict(
        allowed=True,
        action=RemediationAction.RESTART_DEPLOYMENT,
        detail="authorised for test",
        adjusted_params=ActionParams(namespace="citizen-portal", deployment="citizen-service", replicas=2),
        checks={"namespace_allowed": True, "confidence": True},
    )
    result = RemediationResult(
        action=RemediationAction.RESTART_DEPLOYMENT,
        params=plan.params,
        succeeded=True,
        detail="restarted",
        dry_run=True,
        started_at=time.time(),
        duration_seconds=1.5,
        before={"replicas": 2},
    )
    validation = ValidationReport(
        outcome=ValidationOutcome.PASSED,
        checks={"http_ok": True},
        failed_checks=[],
        skipped_checks=["p95_latency"],
        elapsed_seconds=4.2,
        detail="recovered",
    )
    attempt = AttemptRecord(plan=plan, verdict=verdict, result=result, validation=validation)

    incident = Incident(
        id="INC-TEST-ROUNDTRIP",
        fingerprint="fp-roundtrip",
        alertname="HighHTTPErrorRate",
        severity=Severity.CRITICAL,
        app="citizen-service",
        namespace="citizen-portal",
        evidence=Evidence(error_rate=0.42, cpu_cores=1.2, correlations=["deploy 3m ago"]),
        hypothesis=Hypothesis(
            root_cause=RootCause.BAD_DEPLOYMENT,
            confidence=0.93,
            reasoning="recent deploy correlates",
            recommended_action=RemediationAction.RESTART_DEPLOYMENT,
        ),
        attempts=[attempt],
    )
    incident.record(incident.phase, "test event", some_detail=1)

    original = incident.to_dict()
    reconstructed = Incident.from_dict(original)
    assert reconstructed.to_dict() == original
