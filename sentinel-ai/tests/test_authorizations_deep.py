"""
Tests for Deep Investigation proposal authorization (routers/authorizations.py's
`list_deep_proposals` / `authorize_deep_proposal` +
Orchestrator.authorize_and_remediate_deep).

Deliberately mirrors tests/test_authorizations.py's own style and fixtures
(`escalated_incident`, `_wait_for_status`) since this is the same shape of
feature — a human authorizing exactly one action against an already-escalated
incident — for a `DeepRemediationProposal` instead of one of the four known
`RemediationAction`s. See app/routers/authorizations.py's module docstring
for why the two endpoints share the same TTL/lease/audit-row machinery.
"""
from __future__ import annotations

import time

import pytest

from tests.test_authorizations import _fire_alert, _wait_for_status, _wait_for_terminal


@pytest.fixture
def escalated_incident(gui_client):
    """Same fixture as test_authorizations.py's own: a real incident that
    escalated with no candidate action, confidence far below any threshold —
    the exact situation both authorization paths exist for."""
    client, login = gui_client
    headers = login(client)
    incident_id = _fire_alert(client, fingerprint="deep-authz-fp-1")
    incident = _wait_for_terminal(client, headers, incident_id)
    assert incident["status"] == "escalated"
    return client, headers, incident_id


def _inject_deep_proposal(client, incident_id, **overrides):
    """Directly injects a SUGGESTED deep proposal onto the stored incident,
    bypassing a real LLM call (this test sandbox has no reasoner configured
    and no cluster, so calling investigate_deep() for real would only ever
    return None — see deep_investigation.py). Mirrors how
    test_authorizations.py's own `test_authorize_requires_the_incident_to_be_
    currently_escalated` fabricates incident state directly through the
    store rather than driving a full lifecycle run for state that is cheap
    and deterministic to construct directly.

    Defaults to a proposal that clears every eligibility gate: matches the
    incident's own namespace/deployment, confidence above the 0.97 deep
    threshold, an allowed namespace/deployment.
    """
    store = client.app.state.store
    incident_dict = store.get_incident(incident_id)
    proposal_id = overrides.pop("id", f"deep-{uuid_hex()}")
    now = time.time()
    proposal = {
        "id": proposal_id,
        "incident_id": incident_id,
        "created_at": now,
        "created_at_iso": "2026-01-01T00:00:00Z",
        "problem": "citizen-service is failing to reach its database",
        "root_cause": "misconfigured DATABASE_HOST env var",
        "action_type": "set_env_var",
        "target": {
            "namespace": "citizen-portal",
            "deployment": "citizen-service",
            "container": "citizen-service",
            "key": "DATABASE_HOST",
            "value": "citizen-postgres.citizen-portal.svc.cluster.local",
            "previous_value": "bad-host",
            "previous_value_existed": True,
        },
        "reason": "evidence shows connection refused errors against 'bad-host'",
        "expected_effect": "database connections succeed and the error rate drops",
        "risk_level": "moderate",
        "risk_reasoning": ["in-scope single workload", "validation available"],
        "reversible": True,
        "validation_plan": "poll error rate for 60s after the change",
        "confidence": 0.99,
        "status": "suggested",
        "rendered_command": (
            "kubectl set env deployment/citizen-service -n citizen-portal "
            "-c citizen-service DATABASE_HOST=citizen-postgres.citizen-portal.svc.cluster.local"
        ),
        "llm_raw": "{}",
        "llm_label": "fake:test",
        "rejected_reason": None,
        "authorization_id": None,
        "result_detail": "",
    }
    proposal.update(overrides)
    incident_dict.setdefault("deep_proposals", [])
    incident_dict["deep_proposals"].append(proposal)
    store.upsert_incident(incident_dict)
    return proposal


def uuid_hex() -> str:
    import uuid

    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# Auth / validation
# ---------------------------------------------------------------------------
def test_list_deep_proposals_requires_auth(gui_client):
    client, _login = gui_client
    resp = client.get("/api/incidents/INC-x/deep-proposals")
    assert resp.status_code == 403


def test_authorize_deep_proposal_requires_auth(gui_client):
    client, _login = gui_client
    resp = client.post("/api/incidents/INC-x/deep-proposals/prop-1/authorize")
    assert resp.status_code == 403


def test_list_deep_proposals_on_nonexistent_incident_is_404(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.get("/api/incidents/does-not-exist/deep-proposals", headers=headers)
    assert resp.status_code == 404


def test_authorize_deep_proposal_on_nonexistent_incident_is_404(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.post(
        "/api/incidents/does-not-exist/deep-proposals/prop-1/authorize", headers=headers
    )
    assert resp.status_code == 404


def test_list_deep_proposals_empty_for_a_fresh_incident(gui_client, escalated_incident):
    client, headers, incident_id = escalated_incident
    resp = client.get(f"/api/incidents/{incident_id}/deep-proposals", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["incident_id"] == incident_id
    assert body["deep_proposals"] == []


def test_list_deep_proposals_returns_an_injected_proposal(gui_client, escalated_incident):
    client, headers, incident_id = escalated_incident
    proposal = _inject_deep_proposal(client, incident_id)
    resp = client.get(f"/api/incidents/{incident_id}/deep-proposals", headers=headers)
    assert resp.status_code == 200
    listed = resp.json()["deep_proposals"]
    assert len(listed) == 1
    assert listed[0]["id"] == proposal["id"]
    assert listed[0]["status"] == "suggested"
    assert listed[0]["action_type"] == "set_env_var"


def test_authorize_does_not_require_the_incident_to_be_currently_escalated(gui_client):
    """A suggested deep proposal on a NON-escalated ('open') incident must
    still be authorizable.

    This is deliberately the opposite of what this test asserted before the
    Suggest Fix flow existed: `orchestrator.suggest_fix` can produce a
    SUGGESTED proposal on an incident that is not, and may never become,
    ESCALATED (see its own docstring), so gating authorization on live
    'escalated' status would make a Suggest-Fix-produced proposal
    permanently unauthorizable. Eligibility here is carried entirely by the
    proposal's own `status == 'suggested'` check (see
    `test_authorize_a_non_suggested_proposal_is_409` below for that half),
    not by the incident's status — exactly like `list_deep_proposals`,
    which has never gated on escalation either."""
    client, login = gui_client
    headers = login(client)

    fake_incident = {
        "id": "INC-FAKE-OPEN-DEEP",
        "fingerprint": "fake-fp-deep",
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
        "deep_proposals": [],
    }
    client.app.state.store.upsert_incident(fake_incident)
    proposal = _inject_deep_proposal(client, "INC-FAKE-OPEN-DEEP")

    resp = client.post(
        f"/api/incidents/INC-FAKE-OPEN-DEEP/deep-proposals/{proposal['id']}/authorize",
        headers=headers,
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["incident_id"] == "INC-FAKE-OPEN-DEEP"
    assert body["proposal_id"] == proposal["id"]

    listed = client.get(
        "/api/incidents/INC-FAKE-OPEN-DEEP/authorizations", headers=headers
    ).json()
    assert len(listed["authorizations"]) == 1
    assert listed["authorizations"][0]["action"] == f"deep_remediation:{proposal['id']}"


def test_authorize_nonexistent_proposal_is_404(gui_client, escalated_incident):
    client, headers, incident_id = escalated_incident
    resp = client.post(
        f"/api/incidents/{incident_id}/deep-proposals/does-not-exist/authorize",
        headers=headers,
    )
    assert resp.status_code == 404


def test_authorize_a_non_suggested_proposal_is_409(gui_client, escalated_incident):
    client, headers, incident_id = escalated_incident
    proposal = _inject_deep_proposal(client, incident_id, status="executed")
    resp = client.post(
        f"/api/incidents/{incident_id}/deep-proposals/{proposal['id']}/authorize",
        headers=headers,
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# The real, full flow
# ---------------------------------------------------------------------------
def test_authorize_deep_proposal_creates_an_audit_row_immediately(gui_client, escalated_incident):
    client, headers, incident_id = escalated_incident
    proposal = _inject_deep_proposal(client, incident_id)
    resp = client.post(
        f"/api/incidents/{incident_id}/deep-proposals/{proposal['id']}/authorize",
        headers=headers,
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["incident_id"] == incident_id
    assert body["proposal_id"] == proposal["id"]
    assert body["action_type"] == "set_env_var"
    assert body["scope"] == "this proposal only"
    assert body["permanent_policy_changed"] is False

    listed = client.get(f"/api/incidents/{incident_id}/authorizations", headers=headers).json()
    assert len(listed["authorizations"]) == 1
    row = listed["authorizations"][0]
    assert row["id"] == body["authorization_id"]
    assert row["action"] == f"deep_remediation:{proposal['id']}"


def test_full_deep_authorization_flow_resolves_the_incident(
    gui_client, escalated_incident, monkeypatch
):
    """The end-to-end guarantee this feature exists for: a human-authorized
    novel action can resolve an incident through the exact same execute ->
    validate pipeline as the four known actions — never a shortcut.

    Same infra limitation and same fix as
    test_authorizations.py::test_full_authorization_flow_resolves_the_incident_via_human_override:
    recovery validation needs a reachable health endpoint this sandbox does
    not have, so `RecoveryValidator.validate` is patched to a deterministic
    PASSED result. Everything upstream — policy's evaluate_deep_proposal,
    the real execute_deep() call under DRY_RUN — is real, unpatched
    production code.
    """
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
    proposal = _inject_deep_proposal(client, incident_id)

    remediation_engine = client.app.state.context.remediation
    original_dry_run = remediation_engine.dry_run
    remediation_engine.dry_run = True
    try:
        resp = client.post(
            f"/api/incidents/{incident_id}/deep-proposals/{proposal['id']}/authorize",
            headers=headers,
        )
        assert resp.status_code == 202

        incident = _wait_for_status(client, headers, incident_id, {"resolved", "escalated"})
    finally:
        remediation_engine.dry_run = original_dry_run

    assert incident["status"] == "resolved"
    assert incident["resolved_at"] is not None

    deep_proposals = incident["deep_proposals"]
    matching = [p for p in deep_proposals if p["id"] == proposal["id"]]
    assert len(matching) == 1
    assert matching[0]["status"] == "validated"
    assert matching[0]["authorization_id"] is not None

    authorizations = client.get(f"/api/incidents/{incident_id}/authorizations", headers=headers).json()
    row = [r for r in authorizations["authorizations"] if r["action"] == f"deep_remediation:{proposal['id']}"][0]
    assert row["consumed_at"] is not None
    assert row["consumed_result"] == "executed_succeeded"


def test_authorize_deep_proposal_denied_by_policy_is_not_consumed(gui_client, escalated_incident):
    """A proposal targeting a deployment other than the incident's own must
    still be denied for a real policy reason (BLAST_RADIUS_EXCEEDS_INCIDENT)
    even after a human authorized it — and since nothing executed, the grant
    must remain unconsumed."""
    client, headers, incident_id = escalated_incident
    proposal = _inject_deep_proposal(
        client, incident_id, target={
            "namespace": "citizen-portal",
            "deployment": "some-other-deployment",
            "container": "citizen-service",
            "key": "DATABASE_HOST",
            "value": "good-host",
            "previous_value": "bad-host",
            "previous_value_existed": True,
        },
    )

    resp = client.post(
        f"/api/incidents/{incident_id}/deep-proposals/{proposal['id']}/authorize",
        headers=headers,
    )
    assert resp.status_code == 202

    incident = _wait_for_status(client, headers, incident_id, {"escalated"})
    assert incident["status"] == "escalated"

    matching = [p for p in incident["deep_proposals"] if p["id"] == proposal["id"]]
    assert len(matching) == 1
    assert matching[0]["status"] == "rejected"
    assert matching[0]["rejected_reason"]

    authorizations = client.get(f"/api/incidents/{incident_id}/authorizations", headers=headers).json()
    row = [r for r in authorizations["authorizations"] if r["action"] == f"deep_remediation:{proposal['id']}"][0]
    assert row["consumed_at"] is None  # not spent — see this test's docstring
