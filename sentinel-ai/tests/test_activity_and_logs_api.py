"""Tests for routers/activity.py (Sentinel Live) and routers/logs.py
(Sentinel Logs) — the two new GUI-facing endpoints. Same style as
test_gui_api.py: assert on the specific status code, and prove the
"investigating" state reflects a REAL incident in the store, not a
hand-waved shape."""
from __future__ import annotations

import json
import time
from pathlib import Path

from app.models.incident import Evidence, Incident, LifecyclePhase, Severity


def _open_incident(store):
    incident = Incident(
        id="INC-ACTIVITY-0001",
        fingerprint="activity-fp-1",
        alertname="HighHTTPErrorRate",
        severity=Severity.CRITICAL,
        app="citizen-service",
        namespace="citizen-portal",
        evidence=Evidence(),
    )
    incident.record(LifecyclePhase.ROOT_CAUSE_ANALYSIS, "Performing RCA")
    incident.record(
        LifecyclePhase.POLICY_CHECK, "Evaluating policy for candidate action: rollback"
    )
    store.upsert_incident(incident.to_dict())
    return incident


# ---------------------------------------------------------------------------
# Auth gating
# ---------------------------------------------------------------------------
def test_activity_status_requires_auth(gui_client):
    client, _login = gui_client
    assert client.get("/api/activity/status").status_code == 403


def test_logs_list_requires_auth(gui_client):
    client, _login = gui_client
    assert client.get("/api/logs").status_code == 403


def test_logs_stream_requires_a_token(gui_client):
    client, _login = gui_client
    resp = client.get("/api/logs/stream")
    assert resp.status_code == 401


def test_logs_stream_rejects_a_garbage_token(gui_client):
    client, _login = gui_client
    resp = client.get("/api/logs/stream", params={"token": "not-a-real-jwt"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Sentinel Live: idle/monitoring state
# ---------------------------------------------------------------------------
def test_activity_status_is_idle_shaped_with_no_open_incidents(gui_client):
    client, login = gui_client
    headers = login(client)

    resp = client.get("/api/activity/status", headers=headers)
    assert resp.status_code == 200
    body = resp.json()

    # No real Prometheus/Loki/Alertmanager reachable in this test
    # environment, so this is honestly "degraded" (every watcher down),
    # not a faked "monitoring" — see routers/activity.py's `_watchers`.
    assert body["state"] in ("monitoring", "degraded")
    assert "watchers" in body
    watcher_names = {w["name"] for w in body["watchers"]}
    assert watcher_names == {"Prometheus", "Loki", "Kubernetes", "Alertmanager"}
    for watcher in body["watchers"]:
        assert isinstance(watcher["connected"], bool)


# ---------------------------------------------------------------------------
# Sentinel Live: investigating state, backed by a real stored incident
# ---------------------------------------------------------------------------
def test_activity_status_reflects_a_real_open_incident(gui_client):
    client, login = gui_client
    headers = login(client)

    import app.main as main_module

    incident = _open_incident(main_module.app.state.store)

    resp = client.get("/api/activity/status", headers=headers)
    assert resp.status_code == 200
    body = resp.json()

    assert body["state"] == "investigating"
    assert body["incident_id"] == incident.id
    assert body["alertname"] == "HighHTTPErrorRate"
    assert body["phase"] == LifecyclePhase.POLICY_CHECK.value
    # The real message Incident.record() wrote for the last phase
    # transition, not an invented label — see orchestrator.py's `_persist`.
    assert body["message"] == "Evaluating policy for candidate action: rollback"
    assert len(body["recent_timeline"]) == 2


def test_activity_status_ignores_terminal_incidents(gui_client):
    """A resolved incident must not make Sentinel Live claim to still be
    investigating it — proves `_find_active_incident` actually filters on
    status, not just "most recent"."""
    client, login = gui_client
    headers = login(client)

    import app.main as main_module

    incident = Incident(
        id="INC-ACTIVITY-RESOLVED",
        fingerprint="activity-fp-resolved",
        alertname="HighHTTPErrorRate",
        severity=Severity.WARNING,
        app="citizen-service",
        evidence=Evidence(),
    )
    incident.record(LifecyclePhase.RECOVERY_VALIDATION, "Recovery confirmed")
    incident.status = incident.status.__class__.RESOLVED
    main_module.app.state.store.upsert_incident(incident.to_dict())

    resp = client.get("/api/activity/status", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] in ("monitoring", "degraded")
    assert body["last_activity"] == incident.updated_at


# ---------------------------------------------------------------------------
# Sentinel Logs
# ---------------------------------------------------------------------------
def test_list_logs_returns_persisted_entries_matching_the_filter(gui_client):
    client, login = gui_client
    headers = login(client)

    import app.main as main_module

    log_dir = Path(main_module.settings.sentinel_log_dir_resolved)
    log_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        {"message": "RCA complete", "level": "INFO", "name": "app.lifecycle.rca",
         "timestamp_epoch": time.time(), "timestamp": "t1"},
        {"message": "policy denied rollback", "level": "WARNING",
         "name": "app.lifecycle.policy", "timestamp_epoch": time.time(), "timestamp": "t2"},
    ]
    with open(log_dir / "sentinel.log", "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")

    resp = client.get("/api/logs", headers=headers, params={"level": "WARNING"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["logs"][0]["message"] == "policy denied rollback"


def test_list_logs_is_empty_shaped_when_nothing_has_been_captured_yet(gui_client):
    client, login = gui_client
    headers = login(client)

    resp = client.get("/api/logs", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"logs": [], "count": 0}
