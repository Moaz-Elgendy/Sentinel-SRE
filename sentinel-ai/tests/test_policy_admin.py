"""
Tests for app/lifecycle/policy_admin.py — the Sentinel Administration &
Tuning Center's policy validation and live-apply logic.

Style matches test_policy.py: every test asserts the SPECIFIC error/behavior,
not just "it failed" — and, like that file's `human_override` suite, several
of these exist purely to prove a boundary holds under an adversarial input,
not to exercise a happy path.
"""
from __future__ import annotations

import pytest

from app.lifecycle.policy import PolicyEngine
from app.lifecycle.policy_admin import (
    CONFIDENCE_FLOOR,
    MAX_REPLICAS_CEILING,
    MIN_REPLICAS_FLOOR,
    EDITABLE_FIELDS,
    apply_diffs,
    validate_changes,
)

FROZEN_DEPLOYMENTS = frozenset({"citizen-postgres", "notification-postgres"})
FROZEN_NAMESPACES = frozenset({"kube-system", "kube-public", "kube-node-lease"})


@pytest.fixture
def engine(policy_config):
    return PolicyEngine(policy_config)


def _validate(config, changes):
    return validate_changes(config, changes, FROZEN_DEPLOYMENTS, FROZEN_NAMESPACES)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_lowering_a_confidence_threshold_is_valid_but_warns(policy_config):
    errors, diffs = _validate(policy_config, {"confidence_rollback": 0.90})
    assert errors == []
    assert len(diffs) == 1
    diff = diffs[0]
    assert diff.field == "confidence_rollback"
    assert diff.old_value == 0.95
    assert diff.new_value == 0.90
    assert diff.warning is not None
    assert "LOWERING" in diff.warning


def test_raising_a_confidence_threshold_has_no_warning(policy_config):
    errors, diffs = _validate(policy_config, {"confidence_restart": 0.95})
    assert errors == []
    assert diffs[0].warning is None


def test_multiple_fields_in_one_change_produce_one_diff_each(policy_config):
    errors, diffs = _validate(
        policy_config,
        {"confidence_restart": 0.92, "action_cooldown_seconds": 60},
    )
    assert errors == []
    assert {d.field for d in diffs} == {"confidence_restart", "action_cooldown_seconds"}


def test_valid_allowed_deployments_change_is_accepted(policy_config):
    errors, diffs = _validate(
        policy_config, {"allowed_deployments": ["citizen-service", "frontend"]}
    )
    assert errors == []
    assert diffs[0].new_value == ["citizen-service", "frontend"]
    # notification-service was removed relative to the fixture's default —
    # removing is never warned about, only adding.
    assert diffs[0].warning is None


def test_adding_to_allowed_deployments_warns(policy_config):
    errors, diffs = _validate(
        policy_config,
        {"allowed_deployments": ["citizen-service", "notification-service", "frontend", "new-svc"]},
    )
    assert errors == []
    assert "new-svc" in diffs[0].warning


# ---------------------------------------------------------------------------
# Confidence bounds — CONFIDENCE_FLOOR/CEILING are non-negotiable
# ---------------------------------------------------------------------------
def test_confidence_below_floor_is_rejected(policy_config):
    errors, diffs = _validate(policy_config, {"confidence_rollback": CONFIDENCE_FLOOR - 0.01})
    assert diffs == []
    assert any("confidence_rollback" in e for e in errors)


def test_confidence_of_zero_is_rejected(policy_config):
    """The one input an admin might genuinely try, thinking 'always
    autonomous' — this must not be allowed to mean 'no confidence check'."""
    errors, diffs = _validate(policy_config, {"confidence_restart": 0.0})
    assert diffs == []
    assert len(errors) == 1


def test_confidence_above_one_is_rejected(policy_config):
    errors, diffs = _validate(policy_config, {"confidence_scale": 1.5})
    assert diffs == []
    assert len(errors) == 1


def test_confidence_exactly_at_floor_and_ceiling_are_both_valid(policy_config):
    errors, diffs = _validate(
        policy_config, {"confidence_restart": CONFIDENCE_FLOOR, "confidence_scale": 1.0}
    )
    assert errors == []
    assert len(diffs) == 2


def test_confidence_as_a_non_numeric_string_is_rejected(policy_config):
    errors, diffs = _validate(policy_config, {"confidence_rollback": "very confident"})
    assert diffs == []
    assert len(errors) == 1


def test_confidence_as_a_bool_is_rejected(policy_config):
    """bool is a subclass of int in Python — worth pinning down explicitly
    so `True` is never silently accepted as `1.0`."""
    errors, diffs = _validate(policy_config, {"confidence_rollback": True})
    assert diffs == []
    assert len(errors) == 1


# ---------------------------------------------------------------------------
# min_replicas — the one absolute floor the request explicitly calls out
# ---------------------------------------------------------------------------
def test_min_replicas_of_zero_is_rejected(policy_config):
    errors, diffs = _validate(policy_config, {"min_replicas": 0})
    assert diffs == []
    assert any("outage" in e for e in errors)


def test_min_replicas_negative_is_rejected(policy_config):
    errors, diffs = _validate(policy_config, {"min_replicas": -1})
    assert diffs == []
    assert any(str(MIN_REPLICAS_FLOOR) in e for e in errors)


def test_min_replicas_cannot_exceed_max_replicas_in_the_same_change(policy_config):
    errors, diffs = _validate(policy_config, {"min_replicas": 5})  # max_replicas stays 3
    assert diffs == []
    assert any("cannot exceed max_replicas" in e for e in errors)


def test_min_and_max_replicas_can_be_raised_together(policy_config):
    errors, diffs = _validate(policy_config, {"min_replicas": 2, "max_replicas": 5})
    assert errors == []
    assert len(diffs) == 2


def test_max_replicas_above_ceiling_is_rejected(policy_config):
    errors, diffs = _validate(policy_config, {"max_replicas": MAX_REPLICAS_CEILING + 1})
    assert diffs == []
    assert any(str(MAX_REPLICAS_CEILING) in e for e in errors)


def test_max_replicas_below_current_min_replicas_is_rejected(policy_config):
    errors, diffs = _validate(policy_config, {"max_replicas": 0})
    assert diffs == []
    assert any("cannot be less than min_replicas" in e for e in errors)


# ---------------------------------------------------------------------------
# Other numeric bounds
# ---------------------------------------------------------------------------
def test_max_actions_per_incident_out_of_bounds_is_rejected(policy_config):
    errors, diffs = _validate(policy_config, {"max_actions_per_incident": 0})
    assert diffs == []
    errors2, diffs2 = _validate(policy_config, {"max_actions_per_incident": 11})
    assert diffs2 == []


def test_action_cooldown_below_floor_is_rejected(policy_config):
    errors, diffs = _validate(policy_config, {"action_cooldown_seconds": 5})
    assert diffs == []
    assert any("action_cooldown_seconds" in e for e in errors)


def test_correlation_window_out_of_bounds_is_rejected(policy_config):
    errors, diffs = _validate(policy_config, {"deployment_correlation_window_minutes": 0})
    assert diffs == []


# ---------------------------------------------------------------------------
# The frozen deny-lists — must be unreachable from this API, period
# ---------------------------------------------------------------------------
def test_denied_deployments_is_not_an_editable_field(policy_config):
    errors, diffs = _validate(policy_config, {"denied_deployments": []})
    assert diffs == []
    assert len(errors) == 1
    assert "denied_deployments" in errors[0]


def test_denied_namespaces_is_not_an_editable_field(policy_config):
    errors, diffs = _validate(policy_config, {"denied_namespaces": ["kube-system"]})
    assert diffs == []
    assert len(errors) == 1


def test_allowed_deployments_cannot_include_a_frozen_deployment(policy_config):
    errors, diffs = _validate(
        policy_config, {"allowed_deployments": ["citizen-service", "citizen-postgres"]}
    )
    assert diffs == []
    assert any("citizen-postgres" in e for e in errors)


def test_allowed_namespaces_cannot_include_a_frozen_namespace(policy_config):
    errors, diffs = _validate(policy_config, {"allowed_namespaces": ["kube-system"]})
    assert diffs == []
    assert any("kube-system" in e for e in errors)


def test_an_arbitrary_unknown_field_is_rejected(policy_config):
    errors, diffs = _validate(policy_config, {"llm_provider": "gemini"})
    assert diffs == []
    assert len(errors) == 1


def test_every_editable_field_has_a_bound_or_frozen_list_check():
    """A future field added to EDITABLE_FIELDS without a corresponding
    branch in validate_changes would silently accept anything for it — this
    just documents the current complete set so that gap is visible in a
    diff if it ever happens."""
    assert set(EDITABLE_FIELDS) == {
        "allowed_namespaces",
        "allowed_deployments",
        "confidence_rollback",
        "confidence_restart",
        "confidence_scale",
        "confidence_chaos_reset",
        "min_replicas",
        "max_replicas",
        "max_actions_per_incident",
        "action_cooldown_seconds",
        "deployment_correlation_window_minutes",
    }


# ---------------------------------------------------------------------------
# apply_diffs — the live mutation itself
# ---------------------------------------------------------------------------
def test_apply_diffs_mutates_the_live_config_in_place(policy_config):
    errors, diffs = _validate(policy_config, {"confidence_rollback": 0.90})
    assert errors == []
    apply_diffs(policy_config, diffs)
    assert policy_config.confidence_rollback == 0.90


def test_apply_diffs_converts_lists_back_to_frozensets(policy_config):
    errors, diffs = _validate(policy_config, {"allowed_deployments": ["citizen-service"]})
    assert errors == []
    apply_diffs(policy_config, diffs)
    assert policy_config.allowed_deployments == frozenset({"citizen-service"})


def test_a_real_policy_engine_evaluation_reflects_an_applied_change_immediately(
    engine, incident, rollback_ready_context
):
    """The end-to-end guarantee this whole feature depends on: mutating the
    live PolicyConfig object changes what the REAL PolicyEngine.evaluate()
    does on its very next call — no rebuild, no restart."""
    from app.models.incident import DenialReason, RemediationAction

    from .conftest import make_plan

    plan = make_plan(RemediationAction.RESTART_DEPLOYMENT, confidence=0.80)
    before = engine.evaluate(incident, plan, rollback_ready_context, 0.0)
    assert before.allowed is False
    assert before.reason is DenialReason.CONFIDENCE_TOO_LOW

    errors, diffs = _validate(engine.config, {"confidence_restart": 0.75})
    assert errors == []
    apply_diffs(engine.config, diffs)

    after = engine.evaluate(incident, plan, rollback_ready_context, 0.0)
    assert after.allowed is True
