"""
Tests for the Sentinel SRE Control Center GUI's read-only backend API.

Style note, matching test_policy.py: assert on the specific status
code/detail, not just "it failed" — a 401 and a 403 and a 500 are different
bugs, and collapsing them to `!= 200` would let the wrong one slip through
unnoticed.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def test_login_succeeds_with_bootstrap_admin(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "test-admin"
    assert body["role"] == "sre_admin"


def test_login_fails_with_wrong_password(gui_client):
    client, _login = gui_client
    resp = client.post(
        "/api/auth/login", json={"username": "test-admin", "password": "wrong"}
    )
    assert resp.status_code == 401


def test_login_fails_with_unknown_username(gui_client):
    client, _login = gui_client
    resp = client.post(
        "/api/auth/login", json={"username": "nobody", "password": "whatever"}
    )
    assert resp.status_code == 401
    # Same message as a wrong password — must not reveal whether the
    # username exists.
    assert resp.json()["detail"] == "Incorrect username or password"


def test_me_requires_a_token(gui_client):
    client, _login = gui_client
    resp = client.get("/api/auth/me")
    assert resp.status_code == 403  # HTTPBearer's own "no credentials" response


def test_me_rejects_a_garbage_token(gui_client):
    client, _login = gui_client
    resp = client.get(
        "/api/auth/me", headers={"Authorization": "Bearer not-a-real-jwt"}
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Every GUI-facing router requires auth (this is the point of the whole
# Phase 1 exercise — see the spec's "SRE ADMIN ACCESS" requirement).
# ---------------------------------------------------------------------------
def test_incidents_requires_auth(gui_client):
    client, _login = gui_client
    assert client.get("/api/incidents").status_code == 403


def test_environments_requires_auth(gui_client):
    client, _login = gui_client
    assert client.get("/environments").status_code == 403


def test_dashboard_requires_auth(gui_client):
    client, _login = gui_client
    assert client.get("/api/dashboard/summary").status_code == 403


def test_actions_requires_auth(gui_client):
    client, _login = gui_client
    assert client.get("/api/actions").status_code == 403


def test_performance_requires_auth(gui_client):
    client, _login = gui_client
    assert client.get("/api/performance/summary").status_code == 403


def test_meta_requires_auth(gui_client):
    client, _login = gui_client
    assert client.get("/api/meta/root-causes").status_code == 403


def test_alerts_webhook_is_unaffected_by_gui_auth(gui_client):
    """The Alertmanager webhook must stay public — see routers/alerts.py's
    own docstring on why it has no auth. GUI auth must never leak onto it."""
    client, _login = gui_client
    resp = client.post(
        "/api/alerts/webhook",
        json={"version": "4", "status": "firing", "alerts": []},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Meta lookups mirror the real, closed enums — never a hand-duplicated list
# ---------------------------------------------------------------------------
def test_meta_actions_matches_the_closed_remediation_enum(gui_client):
    from app.models.incident import RemediationAction

    client, login = gui_client
    headers = login(client)
    resp = client.get("/api/meta/actions", headers=headers)
    assert resp.status_code == 200
    assert set(resp.json()["actions"]) == {a.value for a in RemediationAction}


def test_meta_root_causes_matches_the_bounded_enum(gui_client):
    from app.models.incident import RootCause

    client, login = gui_client
    headers = login(client)
    resp = client.get("/api/meta/root-causes", headers=headers)
    assert resp.status_code == 200
    assert set(resp.json()["root_causes"]) == {rc.value for rc in RootCause}


def test_meta_lifecycle_phases_primary_flow_excludes_branch_phases(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.get("/api/meta/lifecycle-phases", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    # RE_INVESTIGATION and ESCALATION are real phases (an incident can visit
    # them) but are branches off the spine, not additional steps in it — see
    # the "Live Sentinel Operations" flow diagram this backs.
    assert "re_investigation" not in body["primary_flow_order"]
    assert "escalation" not in body["primary_flow_order"]
    assert body["primary_flow_order"][0] == "detection"
    assert body["primary_flow_order"][-1] == "documentation"


# ---------------------------------------------------------------------------
# Dashboard / actions / performance on an empty store
# ---------------------------------------------------------------------------
def test_dashboard_summary_on_empty_store(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.get("/api/dashboard/summary", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["incidents"]["total_incidents"] == 0
    assert body["incidents"]["active_incidents"] == 0
    assert body["incidents"]["recent_incidents"] == []
    # No in-cluster ServiceAccount in the test environment -> k8s.available
    # is False -> every service reports healthy: null, never a guessed True.
    assert body["sentinel"]["kubernetes_available"] is False
    for service in body["system_health"]["services"]:
        assert service["healthy"] is None
    assert body["latest_config_change"] is None


def test_dashboard_summary_reflects_the_most_recent_applied_config_change(gui_client):
    client, login = gui_client
    headers = login(client)

    client.post(
        "/api/config/monitoring/apply",
        json={"changes": {"prometheus_url": "http://prometheus.new:9090"}},
        headers=headers,
    )
    # A rejected attempt right after it must NOT overwrite the "latest
    # applied" answer with something that never actually changed.
    client.post(
        "/api/config/monitoring/apply",
        json={"changes": {"prometheus_timeout_seconds": 999}},
        headers=headers,
    )

    body = client.get("/api/dashboard/summary", headers=headers).json()
    assert body["latest_config_change"]["category"] == "monitoring"
    assert isinstance(body["latest_config_change"]["changed_by"], str)
    assert body["latest_config_change"]["changed_by"]
    assert body["latest_config_change"]["changed_at"] is not None


def test_actions_on_empty_store(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.get("/api/actions", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"count": 0, "limit": 100, "offset": 0, "actions": []}


def test_performance_on_empty_store_reports_null_not_zero_for_ungrounded_metrics(gui_client):
    """Zero incidents means 'no data', not 'zero accuracy' — the endpoint
    must not fabricate a rate from an empty sample."""
    client, login = gui_client
    headers = login(client)
    resp = client.get("/api/performance/summary", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["remediation_success_rate"] is None
    assert body["avg_incident_resolution_seconds"] is None
    assert body["diagnosis_accuracy"] is None
    assert body["temporary_overrides"] is None
    assert body["escalations"] == 0
    assert body["autonomous_resolutions"] == 0


# ---------------------------------------------------------------------------
# End to end: a real alert -> a real (escalated) incident -> every GUI
# endpoint reflects it consistently.
# ---------------------------------------------------------------------------
def _fire_alert(client, fingerprint="test-fp-1", app="citizen-service"):
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


def test_full_flow_escalated_incident_is_consistent_across_gui_endpoints(gui_client):
    """No Kubernetes/Prometheus/Loki reachable in this test environment, so
    the lifecycle should escalate (no safe action) — this exercises exactly
    the escalation path the GUI's "Temporary Human Authorization" screen
    (a later phase) will render, using only endpoints that exist today."""
    client, login = gui_client
    headers = login(client)

    incident_id = _fire_alert(client)
    incident = _wait_for_terminal(client, headers, incident_id)

    assert incident["status"] == "escalated"
    assert incident["escalated"] is True
    # The mandated lifecycle order (spec section 3) must be present and in
    # order, even on the escalation branch.
    phases = [event["phase"] for event in incident["timeline"]]
    assert phases[0] == "detection"
    assert "root_cause_analysis" in phases
    assert "escalation" in phases

    dashboard = client.get("/api/dashboard/summary", headers=headers).json()
    assert dashboard["incidents"]["total_incidents"] == 1
    assert dashboard["incidents"]["escalated_incidents"] == 1
    assert dashboard["incidents"]["active_incidents"] == 0  # escalated is terminal
    assert dashboard["incidents"]["recent_incidents"][0]["id"] == incident_id

    actions = client.get("/api/actions", headers=headers).json()
    assert actions["count"] == 1
    assert actions["actions"][0]["action"] == "escalate"
    assert actions["actions"][0]["incident_id"] == incident_id
    assert actions["actions"][0]["authorization_type"] == "autonomous"

    perf = client.get("/api/performance/summary", headers=headers).json()
    assert perf["escalations"] == 1
    assert perf["autonomous_resolutions"] == 0

    # Filtering works and excludes what shouldn't match.
    filtered = client.get(
        "/api/actions", params={"action": "restart_deployment"}, headers=headers
    ).json()
    assert filtered["count"] == 0


def test_incidents_list_omits_heavy_fields_but_detail_includes_them(gui_client):
    client, login = gui_client
    headers = login(client)
    incident_id = _fire_alert(client, fingerprint="test-fp-2")
    _wait_for_terminal(client, headers, incident_id)

    listing = client.get("/api/incidents", headers=headers).json()
    assert "evidence" not in listing["incidents"][0]

    detail = client.get(f"/api/incidents/{incident_id}", headers=headers).json()
    assert "evidence" in detail


# ---------------------------------------------------------------------------
# Incident replay / what-if simulation
# ---------------------------------------------------------------------------
def test_replay_endpoint_requires_auth(gui_client):
    client, _login = gui_client
    assert client.get("/api/incidents/INC-x/replay").status_code == 403


def test_replay_endpoint_on_a_nonexistent_incident_is_404(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.get("/api/incidents/does-not-exist/replay", headers=headers)
    assert resp.status_code == 404


def test_replay_endpoint_is_wired_end_to_end_against_a_real_incident(gui_client):
    """Full-app smoke test: exercises every `ctx.*` attribute the endpoint
    reads (settings, policy, validator, chaos) against the real object graph
    main.py builds, which a module-level replay.py test cannot catch (a typo
    in an attribute name there would only ever surface here)."""
    client, login = gui_client
    headers = login(client)
    incident_id = _fire_alert(client, fingerprint="replay-fp-1")
    _wait_for_terminal(client, headers, incident_id)

    resp = client.get(f"/api/incidents/{incident_id}/replay", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["incident_id"] == incident_id
    assert body["from_scratch"] is False
    assert "candidates" in body
    assert "would_escalate" in body
    assert "hypothesis" in body

    resp2 = client.get(
        f"/api/incidents/{incident_id}/replay",
        params={"from_scratch": "true"},
        headers=headers,
    )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["from_scratch"] is True


# ---------------------------------------------------------------------------
# Sentinel Agent Evaluation
# ---------------------------------------------------------------------------
def test_evaluation_endpoints_require_auth(gui_client):
    client, _login = gui_client
    assert client.post("/api/evaluation/runs", json={"scenario": "memory-leak"}).status_code == 403
    assert client.get("/api/evaluation/runs").status_code == 403
    assert client.get("/api/evaluation/summary").status_code == 403


def test_creating_a_run_for_an_unknown_scenario_is_404(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.post(
        "/api/evaluation/runs", json={"scenario": "does-not-exist"}, headers=headers
    )
    assert resp.status_code == 404


def test_creating_a_run_for_the_suite_scenario_is_rejected(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.post("/api/evaluation/runs", json={"scenario": "all"}, headers=headers)
    assert resp.status_code == 422


def test_evaluation_run_lifecycle_end_to_end(gui_client):
    """Full-app smoke test: create a run for a scenario with known ground
    truth, using its default app (no request override needed), then confirm
    it shows up pending in both /runs and /summary."""
    client, login = gui_client
    headers = login(client)

    resp = client.post(
        "/api/evaluation/runs", json={"scenario": "memory-leak"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    run = resp.json()
    assert run["scenario"] == "memory-leak"
    assert run["app"] == "citizen-service"
    assert run["expected_root_cause"] == "memory_leak"
    assert run["resolved"] is False

    listing = client.get("/api/evaluation/runs", headers=headers).json()
    assert listing["count"] == 1
    assert listing["runs"][0]["id"] == run["id"]
    # No incident has happened yet on this app, so it is still pending -
    # never silently scored as wrong.
    assert listing["runs"][0]["resolved"] is False

    summary = client.get("/api/evaluation/summary", headers=headers).json()
    assert summary["sample_size"]["total_runs"] == 1
    assert summary["sample_size"]["pending_runs"] == 1
    assert summary["sample_size"]["concluded_runs"] == 0


def test_evaluation_run_resolves_against_a_real_incident(gui_client):
    """Fires a real alert (same helper the incident tests use) on the same
    app an evaluation run is watching, and confirms the run resolves and
    scores itself once that incident reaches a terminal state - proving the
    store-level linking (find_incident_after) actually works end to end."""
    client, login = gui_client
    headers = login(client)

    run = client.post(
        "/api/evaluation/runs", json={"scenario": "db-outage"}, headers=headers
    ).json()

    incident_id = _fire_alert(client, fingerprint="eval-link-fp-1", app="citizen-service")
    _wait_for_terminal(client, headers, incident_id)

    listing = client.get("/api/evaluation/runs", headers=headers).json()
    resolved = next(r for r in listing["runs"] if r["id"] == run["id"])
    assert resolved["resolved"] is True
    assert resolved["incident_id"] == incident_id
    # No Kubernetes/Prometheus/Loki reachable in this test environment (see
    # test_full_flow_escalated_incident_is_consistent_across_gui_endpoints),
    # so the real RCA outcome will not be chaos_database_fault - the point
    # here is only that linking and scoring ran, not what they concluded.
    assert resolved["root_cause_correct"] is False


# ---------------------------------------------------------------------------
# Diagnosis / remediation feedback (Phase C)
# ---------------------------------------------------------------------------
def test_feedback_endpoints_require_auth(gui_client):
    client, _login = gui_client
    assert client.post("/api/incidents/INC-x/feedback/diagnosis", json={"correct": True}).status_code == 403
    assert client.post("/api/incidents/INC-x/feedback/remediation", json={"useful": True}).status_code == 403
    assert client.get("/api/incidents/INC-x/feedback").status_code == 403


def test_feedback_on_a_nonexistent_incident_is_404(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.post(
        "/api/incidents/does-not-exist/feedback/diagnosis",
        json={"correct": True},
        headers=headers,
    )
    assert resp.status_code == 404


def test_diagnosis_feedback_yes_does_not_require_a_correction(gui_client):
    client, login = gui_client
    headers = login(client)
    incident_id = _fire_alert(client, fingerprint="fb-fp-1")
    _wait_for_terminal(client, headers, incident_id)

    resp = client.post(
        f"/api/incidents/{incident_id}/feedback/diagnosis",
        json={"correct": True, "note": "matches what we saw"},
        headers=headers,
    )
    assert resp.status_code == 201

    listed = client.get(f"/api/incidents/{incident_id}/feedback", headers=headers).json()
    assert len(listed["feedback"]) == 1
    row = listed["feedback"][0]
    assert row["kind"] == "diagnosis"
    assert row["correct_or_useful"] is True
    assert row["corrected_value"] is None
    assert row["note"] == "matches what we saw"


def test_diagnosis_feedback_no_requires_a_correction_from_the_real_enum(gui_client):
    client, login = gui_client
    headers = login(client)
    incident_id = _fire_alert(client, fingerprint="fb-fp-2")
    _wait_for_terminal(client, headers, incident_id)

    # Missing the required correction on a "no" answer.
    resp = client.post(
        f"/api/incidents/{incident_id}/feedback/diagnosis",
        json={"correct": False},
        headers=headers,
    )
    assert resp.status_code == 422

    # A value Sentinel's own RootCause enum does not contain is rejected by
    # request validation, before it ever reaches the store.
    resp = client.post(
        f"/api/incidents/{incident_id}/feedback/diagnosis",
        json={"correct": False, "actual_root_cause": "not_a_real_root_cause"},
        headers=headers,
    )
    assert resp.status_code == 422

    resp = client.post(
        f"/api/incidents/{incident_id}/feedback/diagnosis",
        json={"correct": False, "actual_root_cause": "database_failure"},
        headers=headers,
    )
    assert resp.status_code == 201

    row = client.get(f"/api/incidents/{incident_id}/feedback", headers=headers).json()["feedback"][0]
    assert row["correct_or_useful"] is False
    assert row["corrected_value"] == "database_failure"


def test_remediation_feedback_no_requires_a_suggested_action(gui_client):
    client, login = gui_client
    headers = login(client)
    incident_id = _fire_alert(client, fingerprint="fb-fp-3")
    _wait_for_terminal(client, headers, incident_id)

    resp = client.post(
        f"/api/incidents/{incident_id}/feedback/remediation",
        json={"useful": False},
        headers=headers,
    )
    assert resp.status_code == 422

    resp = client.post(
        f"/api/incidents/{incident_id}/feedback/remediation",
        json={"useful": False, "suggested_action": "rollback_deployment", "note": "should have rolled back"},
        headers=headers,
    )
    assert resp.status_code == 201

    row = client.get(f"/api/incidents/{incident_id}/feedback", headers=headers).json()["feedback"][0]
    assert row["kind"] == "remediation"
    assert row["corrected_value"] == "rollback_deployment"


def test_feedback_is_append_only_and_performance_uses_only_the_latest_per_incident(gui_client):
    """Two rounds of diagnosis feedback on the same incident (a revised SRE
    judgment) must both be stored, but performance/summary must only count
    the most recent one — see performance.py's module docstring."""
    client, login = gui_client
    headers = login(client)
    incident_id = _fire_alert(client, fingerprint="fb-fp-4")
    _wait_for_terminal(client, headers, incident_id)

    client.post(
        f"/api/incidents/{incident_id}/feedback/diagnosis",
        json={"correct": False, "actual_root_cause": "cpu_saturation"},
        headers=headers,
    )
    client.post(
        f"/api/incidents/{incident_id}/feedback/diagnosis",
        json={"correct": True, "note": "actually it was right after all"},
        headers=headers,
    )

    feedback = client.get(f"/api/incidents/{incident_id}/feedback", headers=headers).json()["feedback"]
    assert len(feedback) == 2  # both rounds kept — append-only

    perf = client.get("/api/performance/summary", headers=headers).json()
    # Only the latest ("correct": true) round should count.
    assert perf["diagnosis_accuracy"] == 1.0
    assert perf["incorrect_diagnoses"] == 0
    assert perf["sample_size"]["diagnosis_feedback_count"] == 1


def test_performance_diagnosis_accuracy_is_null_with_no_feedback(gui_client):
    client, login = gui_client
    headers = login(client)
    perf = client.get("/api/performance/summary", headers=headers).json()
    assert perf["diagnosis_accuracy"] is None
    assert perf["diagnosis_feedback_unavailable_reason"] == "no diagnosis feedback recorded yet"


def test_performance_evaluation_metrics_distinguish_failure_modes(gui_client):
    """Seeds hand-crafted incidents directly into the store (bypassing the
    live lifecycle, which this test environment cannot drive through a real
    remediation — no Kubernetes/Prometheus reachable) so each of the new
    rates can be pinned to an exact expected number, not just "not null"."""
    from app.models.incident import (
        ActionParams,
        ActionPlan,
        AttemptRecord,
        Evidence,
        Incident,
        IncidentStatus,
        LifecyclePhase,
        PolicyVerdict,
        RemediationAction,
        RemediationResult,
        Severity,
        TimelineEvent,
        ValidationOutcome,
        ValidationReport,
    )

    client, login = gui_client
    headers = login(client)
    store = client.app.state.store

    def plan(action=RemediationAction.RESTART_DEPLOYMENT):
        return ActionPlan(
            action=action,
            params=ActionParams(namespace="citizen-portal", deployment="citizen-service"),
            confidence=0.95,
            rationale="test",
        )

    def verdict(allowed):
        return PolicyVerdict(allowed=allowed, action=RemediationAction.RESTART_DEPLOYMENT)

    def result(succeeded):
        return RemediationResult(
            action=RemediationAction.RESTART_DEPLOYMENT,
            params=plan().params,
            succeeded=succeeded,
            started_at=1_700_000_100.0,
        )

    def validation(outcome):
        return ValidationReport(outcome=outcome) if outcome else None

    def timeline(created_at, rca_offset):
        return [
            TimelineEvent(phase=LifecyclePhase.DETECTION, message="detected", at=created_at),
            TimelineEvent(
                phase=LifecyclePhase.ROOT_CAUSE_ANALYSIS, message="diagnosed",
                at=created_at + rca_offset,
            ),
        ]

    # A: one denied candidate, then one executed+validated-passed candidate.
    # first_executed_attempt = the passed one -> counts toward
    # first_action_success_rate; verdicted_attempts gets both (1 denied / 2).
    incident_a = Incident(
        id="INC-PERF-A", fingerprint="fp-a", alertname="HighHTTPErrorRate",
        severity=Severity.CRITICAL, app="citizen-service", namespace="citizen-portal",
        status=IncidentStatus.RESOLVED, created_at=1_700_000_000.0, resolved_at=1_700_000_200.0,
        evidence=Evidence(), timeline=timeline(1_700_000_000.0, 4.0),
        attempts=[
            AttemptRecord(plan=plan(), verdict=verdict(False), result=None),
            AttemptRecord(plan=plan(), verdict=verdict(True), result=result(True),
                          validation=validation(ValidationOutcome.PASSED)),
        ],
    )
    # B: executed, applied fine, but validation failed - "ineffective", not
    # an execution failure.
    incident_b = Incident(
        id="INC-PERF-B", fingerprint="fp-b", alertname="HighHTTPErrorRate",
        severity=Severity.CRITICAL, app="citizen-service", namespace="citizen-portal",
        status=IncidentStatus.ESCALATED, escalated=True,
        created_at=1_700_001_000.0, resolved_at=1_700_001_300.0,
        evidence=Evidence(), timeline=timeline(1_700_001_000.0, 6.0),
        attempts=[
            AttemptRecord(plan=plan(), verdict=verdict(True), result=result(True),
                          validation=validation(ValidationOutcome.FAILED)),
        ],
    )
    # C: the action itself failed to apply - an execution failure, distinct
    # from B's "applied but ineffective".
    incident_c = Incident(
        id="INC-PERF-C", fingerprint="fp-c", alertname="HighHTTPErrorRate",
        severity=Severity.CRITICAL, app="citizen-service", namespace="citizen-portal",
        status=IncidentStatus.ESCALATED, escalated=True,
        created_at=1_700_002_000.0,
        evidence=Evidence(), timeline=[],
        attempts=[AttemptRecord(plan=plan(), verdict=verdict(True), result=result(False))],
    )
    # D: applied fine, but validation was UNAVAILABLE - must be excluded
    # from both the ineffective-remediation and recovery-validation rates,
    # not counted against Sentinel either way.
    incident_d = Incident(
        id="INC-PERF-D", fingerprint="fp-d", alertname="HighHTTPErrorRate",
        severity=Severity.CRITICAL, app="citizen-service", namespace="citizen-portal",
        status=IncidentStatus.RESOLVED,
        created_at=1_700_003_000.0, resolved_at=1_700_003_100.0,
        evidence=Evidence(), timeline=[],
        attempts=[
            AttemptRecord(plan=plan(), verdict=verdict(True), result=result(True),
                          validation=validation(ValidationOutcome.UNAVAILABLE)),
        ],
    )
    for incident in (incident_a, incident_b, incident_c, incident_d):
        store.upsert_incident(incident.to_dict())

    perf = client.get("/api/performance/summary", headers=headers).json()

    # first_action_success_rate: incidents A, B, C, D each have exactly one
    # FIRST executed attempt; only A's passed validation -> 1 of 4.
    assert perf["first_action_success_rate"] == 0.25
    # execution_failure_rate: of 4 executed attempts total (A's second
    # attempt, B, C, D), only C's failed to apply -> 1 of 4.
    assert perf["execution_failure_rate"] == 0.25
    # ineffective_remediation_rate: of attempts that applied successfully
    # AND had a scored validation outcome (A-passed, B-failed; D is
    # excluded as unavailable, C never applied) -> B is the 1 ineffective
    # one of 2 scored.
    assert perf["ineffective_remediation_rate"] == 0.5
    # recovery_validation_success_rate: same denominator as above (A, B) -
    # A passed, B did not -> 1 of 2.
    assert perf["recovery_validation_success_rate"] == 0.5
    # policy_rejection_rate: 5 verdicted attempts total (A's denied + A's
    # allowed + B + C + D), 1 denied -> 1 of 5.
    assert perf["policy_rejection_rate"] == 0.2
    # escalation_rate: B and C are escalated, of 4 incidents -> 0.5.
    assert perf["escalation_rate"] == 0.5
    # avg_investigation_latency_seconds: only A (4s) and B (6s) recorded a
    # detection+RCA timeline -> mean 5.0.
    assert perf["avg_investigation_latency_seconds"] == 5.0
    assert perf["sample_size"]["verdicted_attempts"] == 5
    assert perf["sample_size"]["validation_scored_attempts"] == 2


# ---------------------------------------------------------------------------
# Real-time events (Phase B) — auth is via ?token=, not the Authorization
# header, since the browser's native EventSource cannot set custom headers.
# See routers/events.py's module docstring.
# ---------------------------------------------------------------------------
def test_events_stream_requires_a_token(gui_client):
    client, _login = gui_client
    resp = client.get("/api/events")
    assert resp.status_code == 401


def test_events_stream_rejects_a_garbage_token(gui_client):
    client, _login = gui_client
    resp = client.get("/api/events", params={"token": "not-a-real-jwt"})
    assert resp.status_code == 401


def test_events_stream_rejects_a_bearer_header_alone(gui_client):
    """The Authorization header is not read by this endpoint at all — only
    the ?token= query param is. This pins that down explicitly so a future
    edit can't quietly make the two auth paths inconsistent."""
    client, login = gui_client
    headers = login(client)
    resp = client.get("/api/events", headers=headers)
    assert resp.status_code == 401
