"""
Integration tests for the Sentinel Administration & Tuning Center's policy
configuration API (app/routers/config.py). Unit-level bounds/validation
already has its own thorough suite in test_policy_admin.py — these tests
exist for what only a real HTTP request through the real app can prove:
auth gating, the preview/apply split, audit persistence, and — the one
that matters most — that a change survives a process restart.
"""
from __future__ import annotations


def test_get_policy_requires_auth(gui_client):
    client, _login = gui_client
    assert client.get("/api/config/policy").status_code == 403


def test_apply_requires_auth(gui_client):
    client, _login = gui_client
    resp = client.post("/api/config/policy/apply", json={"changes": {"confidence_restart": 0.8}})
    assert resp.status_code == 403


def test_get_policy_returns_current_values_and_bounds(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.get("/api/config/policy", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "confidence_rollback" in body["current"]
    assert "confidence_rollback" in body["bounds"]
    assert body["bounds"]["confidence_rollback"]["min"] == 0.5
    assert body["last_changed_at"] is None  # nothing applied yet
    # The frozen deny-lists are visible for transparency...
    assert "citizen-postgres" in body["protected"]["denied_deployments"]


def test_get_policy_never_exposes_a_way_to_submit_protected_fields_as_current(gui_client):
    """The 'protected' block is informational only -- confirms it is a
    clearly separate key from 'current', not merged into the editable
    snapshot where a naive client might round-trip it back on an apply."""
    client, login = gui_client
    headers = login(client)
    body = client.get("/api/config/policy", headers=headers).json()
    assert "denied_deployments" not in body["current"]
    assert "denied_namespaces" not in body["current"]


def test_preview_does_not_change_anything(gui_client):
    client, login = gui_client
    headers = login(client)

    before = client.get("/api/config/policy", headers=headers).json()["current"]
    resp = client.post(
        "/api/config/policy/preview",
        json={"changes": {"confidence_rollback": 0.90}},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["diff"][0]["old_value"] == 0.95
    assert body["diff"][0]["new_value"] == 0.90
    assert "LOWERING" in body["diff"][0]["warning"]

    after = client.get("/api/config/policy", headers=headers).json()["current"]
    assert after == before  # unchanged -- preview never applies


def test_preview_of_an_invalid_change_reports_errors_without_a_diff(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.post(
        "/api/config/policy/preview",
        json={"changes": {"min_replicas": 0}},
        headers=headers,
    )
    body = resp.json()
    assert body["valid"] is False
    assert body["diff"] == []
    assert any("outage" in e for e in body["errors"])


def test_apply_with_no_changes_is_rejected(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.post("/api/config/policy/apply", json={"changes": {}}, headers=headers)
    assert resp.status_code == 422


def test_apply_updates_the_live_config_and_is_visible_on_the_next_get(gui_client):
    client, login = gui_client
    headers = login(client)

    resp = client.post(
        "/api/config/policy/apply",
        json={"changes": {"confidence_rollback": 0.90}, "reason": "testing improved autonomy"},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"][0]["new_value"] == 0.90
    assert body["current"]["confidence_rollback"] == 0.90

    after = client.get("/api/config/policy", headers=headers).json()
    assert after["current"]["confidence_rollback"] == 0.90
    assert after["last_changed_by"] is not None


def test_apply_rejects_an_invalid_change_and_records_it_in_history(gui_client):
    client, login = gui_client
    headers = login(client)

    resp = client.post(
        "/api/config/policy/apply",
        json={"changes": {"min_replicas": -1}, "reason": "oops"},
        headers=headers,
    )
    assert resp.status_code == 422

    history = client.get("/api/config/history", headers=headers).json()["history"]
    assert history[0]["status"] == "rejected"
    assert history[0]["reason"] == "oops"
    assert "outage" in history[0]["detail"]

    # And the live config is genuinely untouched.
    current = client.get("/api/config/policy", headers=headers).json()["current"]
    assert current["min_replicas"] == 1  # fixture default, unchanged


def test_history_records_who_what_when_and_why(gui_client):
    client, login = gui_client
    headers = login(client)

    client.post(
        "/api/config/policy/apply",
        json={
            "changes": {"confidence_restart": 0.92, "action_cooldown_seconds": 45},
            "reason": "loosening restart autonomy for the demo",
        },
        headers=headers,
    )

    history = client.get("/api/config/history", headers=headers).json()["history"]
    assert len(history) == 1
    entry = history[0]
    assert entry["status"] == "applied"
    assert entry["reason"] == "loosening restart autonomy for the demo"
    assert entry["admin_id"]
    assert entry["created_at"]
    fields_changed = {c["field"] for c in entry["changes"]}
    assert fields_changed == {"confidence_restart", "action_cooldown_seconds"}
    for change in entry["changes"]:
        assert "old_value" in change and "new_value" in change


def test_history_is_append_only_across_multiple_applies(gui_client):
    client, login = gui_client
    headers = login(client)

    client.post(
        "/api/config/policy/apply",
        json={"changes": {"confidence_restart": 0.92}},
        headers=headers,
    )
    client.post(
        "/api/config/policy/apply",
        json={"changes": {"confidence_restart": 0.88}},
        headers=headers,
    )

    history = client.get("/api/config/history", headers=headers).json()["history"]
    assert len(history) == 2
    # Most recent first.
    assert history[0]["changes"][0]["new_value"] == 0.88
    assert history[0]["changes"][0]["old_value"] == 0.92
    assert history[1]["changes"][0]["new_value"] == 0.92
    assert history[1]["changes"][0]["old_value"] == 0.90  # original fixture default


def test_restore_reverts_exactly_one_past_change_as_a_new_audited_change(gui_client):
    client, login = gui_client
    headers = login(client)

    apply_resp = client.post(
        "/api/config/policy/apply",
        json={"changes": {"confidence_rollback": 0.90}, "reason": "trying lower autonomy bar"},
        headers=headers,
    )
    change_id = apply_resp.json()["id"]
    assert client.get("/api/config/policy", headers=headers).json()["current"]["confidence_rollback"] == 0.90

    restore_resp = client.post(f"/api/config/history/{change_id}/restore", headers=headers)
    assert restore_resp.status_code == 200
    body = restore_resp.json()
    assert body["restores_change_id"] == change_id
    assert body["current"]["confidence_rollback"] == 0.95  # back to the original

    history = client.get("/api/config/history", headers=headers).json()["history"]
    assert len(history) == 2  # the original apply AND the restore, both kept
    assert history[0]["restores_change_id"] == change_id


def test_restore_of_an_unknown_change_is_404(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.post("/api/config/history/does-not-exist/restore", headers=headers)
    assert resp.status_code == 404


def test_restore_of_a_rejected_change_is_refused(gui_client):
    client, login = gui_client
    headers = login(client)
    client.post(
        "/api/config/policy/apply", json={"changes": {"min_replicas": -1}}, headers=headers
    )
    rejected_id = client.get("/api/config/history", headers=headers).json()["history"][0]["id"]

    resp = client.post(f"/api/config/history/{rejected_id}/restore", headers=headers)
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# The one that matters most: a change must survive a process restart.
# ---------------------------------------------------------------------------
def test_a_policy_change_is_persisted_so_a_restart_can_reload_it(gui_client):
    """Proves persistence end-to-end (apply -> stored override row -> the
    exact reload function main.py's lifespan calls at startup), without the
    real risk of racing two live TestClient/app lifespans against the same
    SQLite file in-process (this codebase's own SSE test hit exactly that
    kind of tooling limitation — see test_gui_api.py's history). This is a
    more direct, more deterministic proof of the same guarantee: it calls
    the actual `reload_stored_overrides` function main.py's lifespan calls,
    against a BRAND NEW PolicyConfig built the same way `build_context` 
    builds one at every real startup, not a hand-rolled substitute."""
    from app.lifecycle.policy import PolicyConfig
    from app.lifecycle.policy_admin import reload_stored_overrides

    client, login = gui_client
    headers = login(client)

    client.post(
        "/api/config/policy/apply",
        json={"changes": {"confidence_scale": 0.80}, "reason": "surviving a restart"},
        headers=headers,
    )

    store = client.app.state.store
    stored = store.get_config_overrides("policy")
    assert stored == {"confidence_scale": 0.80}

    # A fresh PolicyConfig, exactly as `build_context` produces at a real
    # startup — settings-derived defaults, no knowledge of the change above.
    fresh_config = PolicyConfig.from_settings(client.app.state.settings)
    assert fresh_config.confidence_scale != 0.80  # sanity: genuinely fresh

    errors = reload_stored_overrides(
        fresh_config, stored, fresh_config.denied_deployments, fresh_config.denied_namespaces
    )
    assert errors == []
    assert fresh_config.confidence_scale == 0.80


def test_reload_of_an_override_that_no_longer_passes_validation_fails_loud(gui_client):
    """If a bound tightens between one Sentinel version and the next, a
    previously-valid stored override must not be silently reapplied as
    though nothing changed."""
    from app.lifecycle.policy_admin import reload_stored_overrides

    client, login = gui_client
    headers = login(client)
    ctx = client.app.state.context

    # A value that was fine under today's bounds but simulates a tightened
    # rule by being fed straight to reload as if it were stored state.
    errors = reload_stored_overrides(
        ctx.policy.config,
        {"min_replicas": -1},
        ctx.policy.config.denied_deployments,
        ctx.policy.config.denied_namespaces,
    )
    assert errors != []
    assert ctx.policy.config.min_replicas == 1  # untouched — never silently applied
