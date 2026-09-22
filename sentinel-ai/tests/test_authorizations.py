"""
Tests for temporary SRE authorization (routers/authorizations.py +
Orchestrator.authorize_and_remediate).

`DRY_RUN` is forced on for the tests that actually execute an authorized
action: this test environment has no real Kubernetes API server, and
DRY_RUN=true is the same "authorised and fully resolved but not applied to
the cluster" path a real first-run-against-a-live-cluster would use (see
lifecycle/remediation.py's `execute`) — it is not a test-only shortcut that
bypasses anything these tests care about (policy still runs in full before
DRY_RUN is ever consulted).
"""
from __future__ import annotations

import time

import pytest


def _fire_alert(client, fingerprint="authz-fp-1", app="citizen-service"):
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
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/api/incidents/{incident_id}", headers=headers)
        assert resp.status_code == 200
        incident = resp.json()
        if incident["status"] in ("resolved", "escalated", "auto_resolved"):
            return incident
        time.sleep(0.2)
    raise AssertionError(f"incident {incident_id} did not reach a terminal state in time")


def _wait_for_status(client, headers, incident_id, statuses, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/api/incidents/{incident_id}", headers=headers)
        assert resp.status_code == 200
        incident = resp.json()
        if incident["status"] in statuses:
            return incident
        time.sleep(0.2)
    raise AssertionError(f"incident {incident_id} never reached one of {statuses} in time")


@pytest.fixture
def escalated_incident(gui_client):
    """A real incident that escalated with NO candidate action (this test
    environment has no Prometheus/Loki/Kubernetes, so RCA lands on
    root_cause=unknown, confidence far below any threshold) — exactly the
    situation temporary authorization exists for."""
    client, login = gui_client
    headers = login(client)
    incident_id = _fire_alert(client)
    incident = _wait_for_terminal(client, headers, incident_id)
    assert incident["status"] == "escalated"
    assert incident["hypothesis"]["confidence"] < 0.5  # sanity: override is genuinely needed
    return client, headers, incident_id


# ---------------------------------------------------------------------------
# Auth / validation
# ---------------------------------------------------------------------------
def test_authorize_requires_auth(gui_client):
    client, _login = gui_client
    resp = client.post("/api/incidents/INC-x/authorize", json={"action": "restart_deployment"})
    assert resp.status_code == 403


def test_authorize_on_nonexistent_incident_is_404(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.post(
        "/api/incidents/does-not-exist/authorize", json={"action": "restart_deployment"}, headers=headers
    )
    assert resp.status_code == 404


def test_authorize_rejects_escalate_as_the_action(gui_client, escalated_incident):
    client, headers, incident_id = escalated_incident
    resp = client.post(
        f"/api/incidents/{incident_id}/authorize", json={"action": "escalate"}, headers=headers
    )
    assert resp.status_code == 422


def test_authorize_rejects_an_action_outside_the_closed_enum(gui_client, escalated_incident):
    client, headers, incident_id = escalated_incident
    resp = client.post(
        f"/api/incidents/{incident_id}/authorize",
        json={"action": "run_arbitrary_shell_command"},
        headers=headers,
    )
    assert resp.status_code == 422


def test_authorize_requires_the_incident_to_be_currently_escalated(gui_client):
    """A fabricated 'open' incident (bypassing a live lifecycle run, for
    speed and determinism) must be rejected — the 'escalated' check reads
    live status, not any historical flag."""
    client, login = gui_client
    headers = login(client)

    fake_incident = {
        "id": "INC-FAKE-OPEN",
        "fingerprint": "fake-fp",
        "alertname": "Test",
        "severity": "critical",
        "app": "citizen-service",
        "namespace": "citizen-portal",
        "status": "open",
        "phase": "detection",
        "created_at": time.time(),
        "updated_at": time.time(),
        "escalated": False,
        "attempts": [],
        "timeline": [],
    }
    client.app.state.store.upsert_incident(fake_incident)

    resp = client.post(
        "/api/incidents/INC-FAKE-OPEN/authorize", json={"action": "restart_deployment"}, headers=headers
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# The real, full flow
# ---------------------------------------------------------------------------
def test_authorize_creates_an_audit_row_immediately(gui_client, escalated_incident):
    client, headers, incident_id = escalated_incident
    resp = client.post(
        f"/api/incidents/{incident_id}/authorize",
        json={"action": "restart_deployment"},
        headers=headers,
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["incident_id"] == incident_id
    assert body["action"] == "restart_deployment"
    assert body["scope"] == "this incident only"
    assert body["permanent_policy_changed"] is False

    listed = client.get(f"/api/incidents/{incident_id}/authorizations", headers=headers).json()
    assert len(listed["authorizations"]) == 1
    row = listed["authorizations"][0]
    assert row["id"] == body["authorization_id"]
    assert row["action"] == "restart_deployment"
    assert row["permanent_policy_changed"] is False


def test_full_authorization_flow_resolves_the_incident_via_human_override(
    gui_client, escalated_incident, monkeypatch
):
    """The end-to-end guarantee this whole feature exists for: an incident
    that autonomously escalated because confidence was too low can be
    resolved once an SRE grants a scoped, one-time exception — and the
    audit trail says exactly that happened.

    Recovery validation itself needs a reachable health endpoint, which this
    test sandbox does not have (same reason `investigate()` sees no
    Prometheus/Loki) — so `RecoveryValidator.validate` is patched to a
    deterministic PASSED result. Everything upstream of it (policy's
    human_override branch, the real execute() call under DRY_RUN) is real,
    unpatched production code; this only replaces the one step that
    otherwise depends on infrastructure this environment cannot provide."""
    import app.lifecycle.validation as validation_module
    from app.models.incident import ValidationOutcome, ValidationReport

    async def _fake_validate(
        self,
        incident,
        params,
        baseline_error_rate=None,
        baseline_p95_latency_seconds=None,
        baseline_cpu_cores=None,
        baseline_memory_bytes=None,
        baseline_deployment=None,
    ):
        return ValidationReport(outcome=ValidationOutcome.PASSED, detail="faked for test")

    monkeypatch.setattr(
        validation_module.RecoveryValidator, "validate", _fake_validate, raising=True
    )

    client, headers, incident_id = escalated_incident

    # `RemediationEngine.dry_run` is captured once at construction (see
    # lifecycle/remediation.py's __init__ docstring: "captured at
    # construction, not read from settings at call time" — deliberate, so a
    # config reload mid-incident cannot change behavior underneath an
    # in-flight remediation). Mutating `settings.dry_run` after startup has
    # no effect on the already-built engine, so the already-constructed
    # instance is patched directly instead.
    remediation_engine = client.app.state.context.remediation
    original_dry_run = remediation_engine.dry_run
    remediation_engine.dry_run = True
    try:
        resp = client.post(
            f"/api/incidents/{incident_id}/authorize",
            json={"action": "restart_deployment"},
            headers=headers,
        )
        assert resp.status_code == 202

        incident = _wait_for_status(client, headers, incident_id, {"resolved", "escalated"})
    finally:
        remediation_engine.dry_run = original_dry_run

    assert incident["status"] == "resolved"
    assert incident["escalated"] is True  # historical fact — it WAS escalated once
    assert incident["resolved_at"] is not None

    authorized_attempts = [
        a for a in incident["attempts"] if "temporary SRE authorization" in (a["plan"].get("rationale") or "")
    ]
    assert len(authorized_attempts) == 1
    attempt = authorized_attempts[0]
    assert attempt["verdict"]["allowed"] is True
    assert attempt["verdict"]["checks"]["confidence_human_override"] is True
    assert attempt["result"]["succeeded"] is True
    assert attempt["result"]["dry_run"] is True

    authorizations = client.get(f"/api/incidents/{incident_id}/authorizations", headers=headers).json()
    row = authorizations["authorizations"][0]
    assert row["consumed_at"] is not None
    assert row["consumed_result"] == "executed_succeeded"

    # This incident correctly does NOT count as an autonomous resolution —
    # a human's authorization made this happen, not Sentinel on its own.
    perf = client.get("/api/performance/summary", headers=headers).json()
    assert perf["autonomous_resolutions"] == 0


def test_authorization_denied_by_policy_is_not_consumed(gui_client, escalated_incident):
    """A scale request for an out-of-band replica count should still be
    denied for a real policy reason even under human_override — and since
    nothing executed, the grant must remain available to try again."""
    client, headers, incident_id = escalated_incident

    resp = client.post(
        f"/api/incidents/{incident_id}/authorize",
        json={"action": "scale_deployment", "replicas": 0},
        headers=headers,
    )
    assert resp.status_code == 202

    incident = _wait_for_status(client, headers, incident_id, {"escalated"})
    assert incident["status"] == "escalated"

    scale_attempts = [a for a in incident["attempts"] if a["plan"]["action"] == "scale_deployment"]
    assert len(scale_attempts) == 1
    assert scale_attempts[0]["verdict"]["allowed"] is False
    assert scale_attempts[0]["result"] is None  # never reached execution

    authorizations = client.get(f"/api/incidents/{incident_id}/authorizations", headers=headers).json()
    row = authorizations["authorizations"][0]
    assert row["consumed_at"] is None  # not spent — see this test's docstring

    # Since the incident is still escalated and the grant is still valid,
    # authorizing again (a different, valid action) must be accepted.
    resp2 = client.post(
        f"/api/incidents/{incident_id}/authorize",
        json={"action": "restart_deployment"},
        headers=headers,
    )
    assert resp2.status_code == 202


def test_actions_endpoint_reports_temporary_authorization_type(gui_client, escalated_incident):
    client, headers, incident_id = escalated_incident

    # No dry_run patching needed here: even a real execution attempt that
    # fails against this sandbox's unreachable Kubernetes still produces a
    # RemediationResult (succeeded=False), which is enough for actions.py to
    # include the row — this test only checks `authorization_type`, not the
    # outcome.
    client.post(
        f"/api/incidents/{incident_id}/authorize",
        json={"action": "restart_deployment"},
        headers=headers,
    )
    _wait_for_status(client, headers, incident_id, {"resolved", "escalated"})

    actions = client.get("/api/actions", headers=headers).json()["actions"]
    restart_rows = [a for a in actions if a["action"] == "restart_deployment"]
    assert len(restart_rows) == 1
    assert restart_rows[0]["authorization_type"] == "temporary_sre_authorization"
