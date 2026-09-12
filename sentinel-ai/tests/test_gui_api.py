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
