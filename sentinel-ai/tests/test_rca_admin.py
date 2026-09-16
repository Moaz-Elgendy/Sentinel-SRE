"""Tests for app/lifecycle/rca_admin.py."""
from __future__ import annotations

import pytest

from app.lifecycle.rca_admin import (
    EDITABLE_FIELDS,
    apply_diffs,
    read_only_summary,
    reload_stored_overrides,
    validate_changes,
)
from app.lifecycle.validation import ValidationThresholds


@pytest.fixture
def thresholds():
    return ValidationThresholds()  # defaults: 0.05, 1.5, 0.9, 700MB, 20s, 180s, 10s


def test_defaults_match_the_real_dataclass_defaults(thresholds):
    assert thresholds.max_error_rate == 0.05
    assert thresholds.timeout_seconds == 180


def test_valid_change_is_accepted_and_warns_when_more_tolerant(thresholds):
    errors, diffs = validate_changes(thresholds, {"max_error_rate": 0.10})
    assert errors == []
    assert diffs[0].old_value == 0.05
    assert diffs[0].new_value == 0.10
    assert "tolerant" in diffs[0].warning


def test_making_a_check_stricter_does_not_warn(thresholds):
    errors, diffs = validate_changes(thresholds, {"max_error_rate": 0.02})
    assert errors == []
    assert diffs[0].warning is None


def test_out_of_bounds_error_rate_is_rejected(thresholds):
    errors, diffs = validate_changes(thresholds, {"max_error_rate": 1.5})
    assert diffs == []
    assert len(errors) == 1


def test_negative_cpu_threshold_is_rejected(thresholds):
    errors, diffs = validate_changes(thresholds, {"max_cpu_cores": -1})
    assert diffs == []


def test_non_numeric_value_is_rejected(thresholds):
    errors, diffs = validate_changes(thresholds, {"max_error_rate": "high"})
    assert diffs == []
    assert len(errors) == 1


def test_bool_is_rejected_even_though_it_is_an_int_subclass(thresholds):
    errors, diffs = validate_changes(thresholds, {"settle_seconds": True})
    assert diffs == []
    assert len(errors) == 1


def test_non_integer_float_for_an_int_field_is_rejected(thresholds):
    errors, diffs = validate_changes(thresholds, {"settle_seconds": 12.5})
    assert diffs == []
    assert len(errors) == 1


def test_unknown_field_is_rejected_with_a_clear_reason(thresholds):
    errors, diffs = validate_changes(thresholds, {"confidence_rollback": 0.9})
    assert diffs == []
    assert "hardcoded" in errors[0] or "not configurable" in errors[0]


# ---------------------------------------------------------------------------
# Cross-field consistency: timeout must stay meaningfully larger than both
# settle and poll interval, in either direction of change.
# ---------------------------------------------------------------------------
def test_timeout_must_exceed_settle_seconds(thresholds):
    errors, diffs = validate_changes(thresholds, {"timeout_seconds": 15})  # settle stays 20
    assert diffs == []
    assert any("settle_seconds" in e for e in errors)


def test_timeout_must_exceed_poll_interval(thresholds):
    thresholds.settle_seconds = 5  # so only the poll-interval relationship is being tested
    errors, diffs = validate_changes(thresholds, {"timeout_seconds": 10})  # poll stays 10, equal counts as "not exceeding"
    assert diffs == []
    assert any("poll_interval_seconds" in e for e in errors)


def test_settle_seconds_cannot_reach_or_exceed_timeout(thresholds):
    errors, diffs = validate_changes(thresholds, {"settle_seconds": 180})  # timeout stays 180
    assert diffs == []


def test_poll_interval_cannot_reach_or_exceed_timeout(thresholds):
    errors, diffs = validate_changes(thresholds, {"poll_interval_seconds": 180})
    assert diffs == []


def test_timeout_settle_and_poll_can_all_change_together_consistently(thresholds):
    errors, diffs = validate_changes(
        thresholds,
        {"timeout_seconds": 300, "settle_seconds": 30, "poll_interval_seconds": 15},
    )
    assert errors == []
    assert len(diffs) == 3


def test_every_editable_field_is_covered_by_bounds():
    from app.lifecycle.rca_admin import BOUNDS

    assert set(EDITABLE_FIELDS) == set(BOUNDS)


# ---------------------------------------------------------------------------
# apply / reload
# ---------------------------------------------------------------------------
def test_apply_diffs_mutates_the_live_object(thresholds):
    errors, diffs = validate_changes(thresholds, {"max_error_rate": 0.10})
    apply_diffs(thresholds, diffs)
    assert thresholds.max_error_rate == 0.10


def test_reload_stored_overrides_applies_valid_state(thresholds):
    errors = reload_stored_overrides(thresholds, {"max_error_rate": 0.08, "timeout_seconds": 240})
    assert errors == []
    assert thresholds.max_error_rate == 0.08
    assert thresholds.timeout_seconds == 240


def test_reload_stored_overrides_fails_loud_and_does_not_touch_the_object(thresholds):
    errors = reload_stored_overrides(thresholds, {"max_error_rate": 5.0})
    assert errors != []
    assert thresholds.max_error_rate == 0.05  # untouched


def test_a_real_correlation_call_reflects_an_applied_threshold_change_immediately(thresholds):
    """The same end-to-end guarantee policy_admin proves for PolicyConfig,
    proven here for ValidationThresholds: correlation.correlate() (via
    orchestrator.py, which now reads ctx.validator.thresholds directly —
    see this module's docstring) sees a live change on its very next call."""
    from app.lifecycle import correlation
    from app.models.incident import Evidence, Incident, Severity

    incident = Incident(id="INC-T", fingerprint="fp", alertname="Test", severity=Severity.CRITICAL)
    evidence = Evidence(error_rate=0.07)  # between the old (0.05) and a raised (0.10) threshold

    before = correlation.correlate(
        incident=incident,
        evidence=evidence,
        correlation_window_minutes=30,
        cpu_threshold_cores=thresholds.max_cpu_cores,
        error_rate_threshold=thresholds.max_error_rate,
        p95_threshold_seconds=thresholds.max_p95_seconds,
    )
    assert before.error_spike is True

    errors, diffs = validate_changes(thresholds, {"max_error_rate": 0.10})
    assert errors == []
    apply_diffs(thresholds, diffs)

    after = correlation.correlate(
        incident=incident,
        evidence=evidence,
        correlation_window_minutes=30,
        cpu_threshold_cores=thresholds.max_cpu_cores,
        error_rate_threshold=thresholds.max_error_rate,
        p95_threshold_seconds=thresholds.max_p95_seconds,
    )
    assert after.error_spike is False


def test_read_only_summary_exposes_the_real_root_causes_and_llm_constants():
    from app.lifecycle.policy import PolicyConfig
    from app.models.incident import RootCause

    config = PolicyConfig(
        allowed_namespaces=frozenset(),
        allowed_deployments=frozenset(),
        denied_deployments=frozenset(),
        denied_namespaces=frozenset(),
        confidence_rollback=0.95,
        confidence_restart=0.9,
        confidence_scale=0.9,
        confidence_chaos_reset=0.9,
        min_replicas=1,
        max_replicas=3,
        max_actions_per_incident=3,
        action_cooldown_seconds=120,
        deployment_correlation_window_minutes=30,
    )
    summary = read_only_summary(config)
    assert set(summary["root_causes"]) == {rc.value for rc in RootCause}
    assert summary["rule_based_detection"]["llm_confidence_ceiling"] == 0.97
    assert summary["deployment_correlation_window_minutes"]["value"] == 30
    assert summary["deployment_correlation_window_minutes"]["edit_via"] == "/api/config/policy"
