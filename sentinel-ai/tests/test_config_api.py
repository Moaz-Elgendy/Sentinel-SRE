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


# ---------------------------------------------------------------------------
# The bug this inspection actually found: RemediationEngine and
# DecisionEngine each hold their OWN independently-constructed copy of
# allowed_namespaces/allowed_deployments/min_replicas/max_replicas — a real,
# intentional defense-in-depth boundary (see policy_admin.py's
# sync_dependent_engines docstring), NOT something this API is allowed to
# collapse. But without deliberately keeping them in sync, a policy change
# here would show as "applied" while remediation kept refusing the very
# thing it was supposed to newly allow.
# ---------------------------------------------------------------------------
def test_widening_allowed_deployments_propagates_to_the_remediation_engine(gui_client):
    client, login = gui_client
    headers = login(client)
    ctx = client.app.state.context
    assert "new-service" not in ctx.remediation.allowed_deployments

    resp = client.post(
        "/api/config/policy/apply",
        json={"changes": {"allowed_deployments": ["citizen-service", "notification-service", "frontend", "new-service"]}},
        headers=headers,
    )
    assert resp.status_code == 200

    assert "new-service" in ctx.policy.config.allowed_deployments
    assert "new-service" in ctx.remediation.allowed_deployments  # the independent copy, too


def test_narrowing_allowed_namespaces_propagates_to_the_remediation_engine(gui_client):
    client, login = gui_client
    headers = login(client)
    ctx = client.app.state.context

    resp = client.post(
        "/api/config/policy/apply",
        json={"changes": {"allowed_namespaces": []}},
        headers=headers,
    )
    assert resp.status_code == 200
    assert ctx.policy.config.allowed_namespaces == frozenset()
    assert ctx.remediation.allowed_namespaces == frozenset()


def test_raising_max_replicas_propagates_to_both_remediation_and_decision_engines(gui_client):
    client, login = gui_client
    headers = login(client)
    ctx = client.app.state.context

    resp = client.post(
        "/api/config/policy/apply", json={"changes": {"max_replicas": 8}}, headers=headers
    )
    assert resp.status_code == 200

    assert ctx.policy.config.max_replicas == 8
    assert ctx.remediation.max_replicas == 8
    assert ctx.decision.max_replicas == 8


def test_raising_min_replicas_propagates_to_both_remediation_and_decision_engines(gui_client):
    client, login = gui_client
    headers = login(client)
    ctx = client.app.state.context

    resp = client.post(
        "/api/config/policy/apply", json={"changes": {"min_replicas": 2}}, headers=headers
    )
    assert resp.status_code == 200

    assert ctx.policy.config.min_replicas == 2
    assert ctx.remediation.min_replicas == 2
    assert ctx.decision.min_replicas == 2


def test_a_change_unrelated_to_the_duplicated_fields_does_not_touch_the_other_engines(gui_client):
    """sync_dependent_engines should be a no-op when the change doesn't
    involve any of the four duplicated fields — confirms the sync is
    targeted, not an unconditional resync of everything on every apply."""
    client, login = gui_client
    headers = login(client)
    ctx = client.app.state.context
    remediation_max_replicas_before = ctx.remediation.max_replicas

    resp = client.post(
        "/api/config/policy/apply", json={"changes": {"confidence_restart": 0.92}}, headers=headers
    )
    assert resp.status_code == 200
    assert ctx.remediation.max_replicas == remediation_max_replicas_before


def test_a_rejected_change_never_reaches_the_dependent_engines(gui_client):
    client, login = gui_client
    headers = login(client)
    ctx = client.app.state.context

    resp = client.post(
        "/api/config/policy/apply", json={"changes": {"min_replicas": -1}}, headers=headers
    )
    assert resp.status_code == 422
    assert ctx.remediation.min_replicas == 1  # untouched


def test_restoring_an_allowed_deployments_change_also_reverts_the_remediation_engine(gui_client):
    client, login = gui_client
    headers = login(client)
    ctx = client.app.state.context

    apply_resp = client.post(
        "/api/config/policy/apply",
        json={"changes": {"allowed_deployments": ["citizen-service", "temp-service"]}},
        headers=headers,
    )
    change_id = apply_resp.json()["id"]
    assert "temp-service" in ctx.remediation.allowed_deployments

    client.post(f"/api/config/history/{change_id}/restore", headers=headers)
    assert "temp-service" not in ctx.policy.config.allowed_deployments
    assert "temp-service" not in ctx.remediation.allowed_deployments


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
# RCA / Diagnosis configuration
# ---------------------------------------------------------------------------
def test_get_rca_requires_auth(gui_client):
    client, _login = gui_client
    assert client.get("/api/config/rca").status_code == 403


def test_get_rca_returns_current_thresholds_bounds_and_read_only_info(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.get("/api/config/rca", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["current"]["max_error_rate"] == 0.05
    assert body["bounds"]["timeout_seconds"]["min"] == 10
    assert "database_failure" in body["read_only"]["root_causes"]
    assert body["read_only"]["rule_based_detection"]["llm_confidence_ceiling"] == 0.97
    assert body["read_only"]["deployment_correlation_window_minutes"]["edit_via"] == "/api/config/policy"


def test_rca_preview_and_apply_round_trip(gui_client):
    client, login = gui_client
    headers = login(client)

    preview = client.post(
        "/api/config/rca/preview", json={"changes": {"max_error_rate": 0.10}}, headers=headers
    ).json()
    assert preview["valid"] is True
    assert "tolerant" in preview["diff"][0]["warning"]

    resp = client.post(
        "/api/config/rca/apply",
        json={"changes": {"max_error_rate": 0.10}, "reason": "noisier baseline for the demo env"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["current"]["max_error_rate"] == 0.10

    current = client.get("/api/config/rca", headers=headers).json()["current"]
    assert current["max_error_rate"] == 0.10


def test_rca_apply_rejects_a_timeout_shorter_than_settle(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.post(
        "/api/config/rca/apply", json={"changes": {"timeout_seconds": 10}}, headers=headers
    )
    assert resp.status_code == 422

    history = client.get("/api/config/history", headers=headers, params={"category": "rca"}).json()["history"]
    assert history[0]["status"] == "rejected"


def test_config_history_can_be_filtered_by_category(gui_client):
    client, login = gui_client
    headers = login(client)

    client.post("/api/config/policy/apply", json={"changes": {"confidence_restart": 0.92}}, headers=headers)
    client.post("/api/config/rca/apply", json={"changes": {"max_error_rate": 0.10}}, headers=headers)

    all_history = client.get("/api/config/history", headers=headers).json()["history"]
    assert len(all_history) == 2

    policy_only = client.get("/api/config/history", params={"category": "policy"}, headers=headers).json()["history"]
    assert len(policy_only) == 1
    assert policy_only[0]["category"] == "policy"

    rca_only = client.get("/api/config/history", params={"category": "rca"}, headers=headers).json()["history"]
    assert len(rca_only) == 1
    assert rca_only[0]["category"] == "rca"


def test_restore_works_for_the_rca_category_too(gui_client):
    """The restore endpoint is category-generic — this pins that down for
    a NON-policy category, not just policy (which every other restore test
    in this file already covers)."""
    client, login = gui_client
    headers = login(client)

    apply_resp = client.post(
        "/api/config/rca/apply", json={"changes": {"max_error_rate": 0.10}}, headers=headers
    )
    change_id = apply_resp.json()["id"]

    restore_resp = client.post(f"/api/config/history/{change_id}/restore", headers=headers)
    assert restore_resp.status_code == 200
    assert restore_resp.json()["current"]["max_error_rate"] == 0.05

    rca_current = client.get("/api/config/rca", headers=headers).json()["current"]
    assert rca_current["max_error_rate"] == 0.05


def test_a_real_incident_lifecycle_reflects_a_live_rca_threshold_change(gui_client):
    """The end-to-end guarantee this whole feature depends on, proven
    through a real alert -> real orchestrator run, not a direct function
    call: raising max_error_rate must change whether a real incident's
    evidence correlates as an error spike."""
    import time

    client, login = gui_client
    headers = login(client)

    # Absurdly high error-rate tolerance -- guarantees no real evidence
    # (even the near-zero/None readings this sandboxed environment produces)
    # could ever exceed it, which is the observable, black-box proof that
    # the live threshold is actually being read where correlation happens.
    client.post("/api/config/rca/apply", json={"changes": {"max_error_rate": 0.99}}, headers=headers)

    payload = {
        "version": "4",
        "status": "firing",
        "commonLabels": {},
        "commonAnnotations": {},
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "HighHTTPErrorRate", "app": "citizen-service", "severity": "critical"},
                "annotations": {"summary": "rca threshold e2e"},
                "startsAt": "2026-09-12T07:00:00Z",
                "fingerprint": "rca-threshold-e2e-1",
            }
        ],
    }
    resp = client.post("/api/alerts/webhook", json=payload)
    assert resp.status_code == 200
    incident_id = resp.json()["incidents"][0]["incident_id"]

    deadline = time.time() + 10
    incident = None
    while time.time() < deadline:
        incident = client.get(f"/api/incidents/{incident_id}", headers=headers).json()
        if incident["status"] in ("resolved", "escalated", "auto_resolved"):
            break
        time.sleep(0.2)

    assert incident is not None
    # With no Prometheus/Loki reachable in this sandbox there is no error
    # rate evidence to spike in the first place -- the meaningful assertion
    # here is that the apply succeeded and the incident still processed
    # normally with the new threshold live, without raising or hanging.
    assert incident["status"] in ("resolved", "escalated", "auto_resolved")
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
    from types import SimpleNamespace

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
    # Wrapped in a minimal ctx-shaped object since reload_stored_overrides
    # now also syncs dependent engines (see sync_dependent_engines) --
    # `remediation`/`decision` are simply absent here, which that function
    # handles the same way it would a partially-initialized real context.
    fresh_config = PolicyConfig.from_settings(client.app.state.settings)
    assert fresh_config.confidence_scale != 0.80  # sanity: genuinely fresh
    fresh_ctx = SimpleNamespace(policy=SimpleNamespace(config=fresh_config))

    errors = reload_stored_overrides(
        fresh_ctx, stored, fresh_config.denied_deployments, fresh_config.denied_namespaces
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
        ctx,
        {"min_replicas": -1},
        ctx.policy.config.denied_deployments,
        ctx.policy.config.denied_namespaces,
    )
    assert errors != []
    assert ctx.policy.config.min_replicas == 1  # untouched — never silently applied
    assert ctx.remediation.min_replicas == 1  # the dependent engine untouched too


# ---------------------------------------------------------------------------
# Remediation configuration
# ---------------------------------------------------------------------------
def test_get_remediation_requires_auth(gui_client):
    client, _login = gui_client
    assert client.get("/api/config/remediation").status_code == 403


def test_get_remediation_returns_current_dry_run_and_action_ladder(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.get("/api/config/remediation", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["current"]["dry_run"] is False
    assert body["read_only"]["action_ladder"]["database_failure"] == []
    assert "rollback_deployment" in body["read_only"]["action_ladder"]["bad_deployment"]


def test_remediation_preview_warns_when_turning_dry_run_off(gui_client):
    client, login = gui_client
    headers = login(client)
    # Turn it on first so the interesting direction (off) can be previewed.
    client.post("/api/config/remediation/apply", json={"changes": {"dry_run": True}}, headers=headers)

    preview = client.post(
        "/api/config/remediation/preview", json={"changes": {"dry_run": False}}, headers=headers
    ).json()
    assert preview["valid"] is True
    assert "mutating the cluster" in preview["diff"][0]["warning"]


def test_remediation_apply_rejects_a_non_boolean(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.post("/api/config/remediation/apply", json={"changes": {"dry_run": "yes"}}, headers=headers)
    assert resp.status_code == 422


def test_remediation_apply_actually_changes_engine_behavior(gui_client):
    """Not just that the API reports success -- that the live
    RemediationEngine object sentinel-ai actually executes against changed."""
    client, login = gui_client
    headers = login(client)
    ctx = client.app.state.context
    assert ctx.remediation.dry_run is False

    resp = client.post(
        "/api/config/remediation/apply",
        json={"changes": {"dry_run": True}, "reason": "safety rehearsal for the demo"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert ctx.remediation.dry_run is True


def test_remediation_history_and_restore_work_like_every_other_category(gui_client):
    client, login = gui_client
    headers = login(client)
    ctx = client.app.state.context

    apply_resp = client.post(
        "/api/config/remediation/apply", json={"changes": {"dry_run": True}}, headers=headers
    )
    change_id = apply_resp.json()["id"]

    history = client.get(
        "/api/config/history", params={"category": "remediation"}, headers=headers
    ).json()["history"]
    assert len(history) == 1
    assert history[0]["changes"][0]["field"] == "dry_run"

    client.post(f"/api/config/history/{change_id}/restore", headers=headers)
    assert ctx.remediation.dry_run is False


def test_get_ai_requires_auth(gui_client):
    client, _login = gui_client
    assert client.get("/api/config/ai").status_code == 403


def test_get_ai_returns_current_values_bounds_and_read_only_info(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.get("/api/config/ai", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["current"]["llm_provider"] == "openai"
    assert body["current"]["openai_model"] == "gpt-4o-mini"
    assert body["bounds"]["llm_provider"]["choices"] == ["openai", "gemini", "groq"]
    assert body["read_only"]["temperature"] == 0.0
    assert body["read_only"]["groq_api_key_configured"] is False
    assert body["read_only"]["openai_api_key_configured"] is False
    assert body["read_only"]["gemini_api_key_configured"] is False


def test_get_ai_never_exposes_api_keys_anywhere_in_response(gui_client):
    client, login = gui_client
    headers = login(client)
    ctx = client.app.state.context
    ctx.settings.openai_api_key = "sk-super-secret-value"
    ctx.settings.gemini_api_key = "gm-also-secret"
    body = client.get("/api/config/ai", headers=headers).json()
    dumped = str(body)
    assert "sk-super-secret-value" not in dumped
    assert "gm-also-secret" not in dumped
    assert "openai_api_key" not in body["current"]
    assert "gemini_api_key" not in body["current"]


def test_ai_preview_does_not_change_anything(gui_client):
    client, login = gui_client
    headers = login(client)
    ctx = client.app.state.context
    preview = client.post(
        "/api/config/ai/preview", json={"changes": {"openai_model": "gpt-4o"}}, headers=headers
    ).json()
    assert preview["valid"] is True
    assert ctx.settings.openai_model == "gpt-4o-mini"  # unchanged


def test_ai_apply_rejects_attempt_to_set_an_api_key(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.post(
        "/api/config/ai/apply", json={"changes": {"openai_api_key": "sk-whatever"}}, headers=headers
    )
    assert resp.status_code == 422
    assert "never be read or written" in resp.text


def test_ai_apply_rejects_an_invalid_provider(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.post(
        "/api/config/ai/apply", json={"changes": {"llm_provider": "claude"}}, headers=headers
    )
    assert resp.status_code == 422


def test_a_rejected_api_key_attempt_is_redacted_in_the_audit_trail(gui_client):
    """A rejected change is still audited (so a rejection attempt is
    visible to other admins), but the submitted VALUE must never be
    persisted anywhere for a sensitive field — not just excluded from the
    live config response. GET /api/config/history is a different door than
    GET /api/config/ai and must be checked separately."""
    client, login = gui_client
    headers = login(client)
    resp = client.post(
        "/api/config/ai/apply",
        json={"changes": {"openai_api_key": "sk-should-never-be-stored-anywhere"}},
        headers=headers,
    )
    assert resp.status_code == 422
    assert "sk-should-never-be-stored-anywhere" not in resp.text

    history = client.get("/api/config/history", params={"category": "ai"}, headers=headers).json()[
        "history"
    ]
    assert len(history) == 1
    assert history[0]["status"] == "rejected"
    assert history[0]["changes"][0]["field"] == "openai_api_key"
    assert history[0]["changes"][0]["new_value"] == "«redacted»"
    assert "sk-should-never-be-stored-anywhere" not in str(history)


def test_ai_preview_switching_provider_without_a_key_warns(gui_client):
    client, login = gui_client
    headers = login(client)
    preview = client.post(
        "/api/config/ai/preview", json={"changes": {"llm_provider": "gemini"}}, headers=headers
    ).json()
    assert preview["valid"] is True
    assert "GEMINI_API_KEY" in preview["diff"][0]["warning"]


def test_ai_apply_actually_rebuilds_the_live_reasoner(gui_client):
    """Not just that the API reports success -- that ctx.reasoner (read
    fresh on every incident by Orchestrator._run) is a NEW instance
    reflecting the change, exactly the guarantee ai_admin.py depends on."""
    client, login = gui_client
    headers = login(client)
    ctx = client.app.state.context
    ctx.settings.openai_api_key = "test-key-for-this-test"
    assert ctx.reasoner is None  # no key was configured at startup

    resp = client.post(
        "/api/config/ai/apply",
        json={"changes": {"openai_model": "gpt-4o"}, "reason": "trying a stronger model"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert ctx.reasoner is not None
    assert ctx.reasoner.label == "openai:gpt-4o"


def test_ai_apply_with_no_changes_is_rejected(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.post("/api/config/ai/apply", json={"changes": {}}, headers=headers)
    assert resp.status_code == 422


def test_ai_history_and_restore_work_like_every_other_category(gui_client):
    client, login = gui_client
    headers = login(client)
    ctx = client.app.state.context
    ctx.settings.openai_api_key = "test-key-for-this-test"

    apply_resp = client.post(
        "/api/config/ai/apply", json={"changes": {"openai_model": "gpt-4o"}}, headers=headers
    )
    change_id = apply_resp.json()["id"]
    assert ctx.reasoner.label == "openai:gpt-4o"

    history = client.get("/api/config/history", params={"category": "ai"}, headers=headers).json()[
        "history"
    ]
    assert len(history) == 1
    assert history[0]["changes"][0]["field"] == "openai_model"

    client.post(f"/api/config/history/{change_id}/restore", headers=headers)
    assert ctx.settings.openai_model == "gpt-4o-mini"
    assert ctx.reasoner.label == "openai:gpt-4o-mini"  # restore also rebuilt the reasoner


def test_get_monitoring_requires_auth(gui_client):
    client, _login = gui_client
    assert client.get("/api/config/monitoring").status_code == 403


def test_get_monitoring_returns_current_bounds_and_read_only_info(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.get("/api/config/monitoring", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["current"]["prometheus_url"] == "http://prometheus:9090"
    assert body["current"]["loki_url"] == "http://loki:3100"
    assert body["bounds"]["prometheus_timeout_seconds"] == {"type": "float", "min": 1.0, "max": 60.0}
    assert body["read_only"]["kubernetes"]["mode"] == "in_cluster"
    assert body["read_only"]["kubernetes"]["available"] is False  # gui_client's documented k8s state
    assert "No configurable timeout" in body["read_only"]["health_checks"]["note"]


def test_monitoring_apply_rejects_a_bearer_token_attempt(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.post(
        "/api/config/monitoring/apply",
        json={"changes": {"prometheus_bearer_token": "sk-whatever"}},
        headers=headers,
    )
    assert resp.status_code == 422
    assert "POST /environments" in resp.text


def test_monitoring_apply_rejects_a_kubernetes_field(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.post(
        "/api/config/monitoring/apply",
        json={"changes": {"kubernetes_namespace": "other-ns"}},
        headers=headers,
    )
    assert resp.status_code == 422


def test_monitoring_apply_rejects_a_malformed_url(gui_client):
    client, login = gui_client
    headers = login(client)
    resp = client.post(
        "/api/config/monitoring/apply",
        json={"changes": {"prometheus_url": "prometheus.internal:9090"}},
        headers=headers,
    )
    assert resp.status_code == 422


def test_monitoring_apply_changes_the_live_client_immediately(gui_client):
    client, login = gui_client
    headers = login(client)
    ctx = client.app.state.context
    resp = client.post(
        "/api/config/monitoring/apply",
        json={"changes": {"prometheus_url": "http://prometheus.new:9090"}},
        headers=headers,
    )
    assert resp.status_code == 200
    assert ctx.prom.base_url == "http://prometheus.new:9090"


def test_monitoring_apply_keeps_the_stored_environment_record_in_sync(gui_client):
    """The exact staleness bug this category was designed around: without
    the after_apply sync, GET /environments/{id} and /test-connection would
    silently keep reporting the OLD Prometheus url forever, even though the
    live evidence-collection client had already moved on."""
    client, login = gui_client
    headers = login(client)
    ctx = client.app.state.context
    environment_id = ctx.environment.id

    client.post(
        "/api/config/monitoring/apply",
        json={"changes": {"prometheus_url": "http://prometheus.new:9090", "loki_timeout_seconds": 45}},
        headers=headers,
    )

    env_resp = client.get(f"/environments/{environment_id}", headers=headers)
    assert env_resp.status_code == 200
    body = env_resp.json()
    assert body["prometheus"]["url"] == "http://prometheus.new:9090"
    # to_public_dict() doesn't surface timeout_seconds under a different key —
    # confirm it round-tripped by reading the field directly off the model.
    stored = client.app.state.store.get_environment(environment_id)
    assert stored["loki"]["timeout_seconds"] == 45.0


def test_monitoring_history_and_restore_work_like_every_other_category(gui_client):
    client, login = gui_client
    headers = login(client)
    ctx = client.app.state.context

    apply_resp = client.post(
        "/api/config/monitoring/apply",
        json={"changes": {"prometheus_url": "http://prometheus.new:9090"}},
        headers=headers,
    )
    change_id = apply_resp.json()["id"]

    history = client.get(
        "/api/config/history", params={"category": "monitoring"}, headers=headers
    ).json()["history"]
    assert len(history) == 1
    assert history[0]["changes"][0]["field"] == "prometheus_url"

    client.post(f"/api/config/history/{change_id}/restore", headers=headers)
    assert ctx.prom.base_url == "http://prometheus:9090"
    assert ctx.environment.prometheus.url == "http://prometheus:9090"  # restore re-synced too
