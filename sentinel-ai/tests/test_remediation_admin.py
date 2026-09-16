"""Tests for app/lifecycle/remediation_admin.py."""
from __future__ import annotations

import pytest

from app.lifecycle.remediation_admin import (
    EDITABLE_FIELDS,
    apply_diffs,
    read_only_summary,
    reload_stored_overrides,
    validate_changes,
)

from .test_remediation_allowlist import build_engine


@pytest.fixture
def remediation():
    return build_engine(dry_run=False)


def test_editable_fields_is_exactly_dry_run():
    assert set(EDITABLE_FIELDS) == {"dry_run"}


def test_turning_dry_run_on_is_valid_and_unwarned(remediation):
    errors, diffs = validate_changes(remediation, {"dry_run": True})
    assert errors == []
    assert diffs[0].old_value is False
    assert diffs[0].new_value is True
    assert diffs[0].warning is None


def test_turning_dry_run_off_warns(remediation):
    remediation.dry_run = True
    errors, diffs = validate_changes(remediation, {"dry_run": False})
    assert errors == []
    assert "OFF" in diffs[0].warning
    assert "mutating the cluster" in diffs[0].warning


def test_setting_dry_run_to_its_current_value_is_a_no_op_diff_without_warning(remediation):
    errors, diffs = validate_changes(remediation, {"dry_run": False})
    assert errors == []
    assert diffs[0].warning is None


def test_non_bool_value_is_rejected(remediation):
    errors, diffs = validate_changes(remediation, {"dry_run": "true"})
    assert diffs == []
    assert len(errors) == 1


def test_an_int_is_rejected_even_though_bool_is_an_int_subclass_in_reverse(remediation):
    """isinstance(1, bool) is False but isinstance(True, int) is True -- this
    pins down that a plain int like 1 is rejected, not silently truthy-cast."""
    errors, diffs = validate_changes(remediation, {"dry_run": 1})
    assert diffs == []
    assert len(errors) == 1


def test_unknown_field_is_rejected_with_a_clear_reason(remediation):
    errors, diffs = validate_changes(remediation, {"allowed_deployments": ["x"]})
    assert diffs == []
    assert "action ladder" in errors[0] or "not configurable" in errors[0]


def test_apply_diffs_mutates_the_live_engine(remediation):
    errors, diffs = validate_changes(remediation, {"dry_run": True})
    apply_diffs(remediation, diffs)
    assert remediation.dry_run is True


def test_reload_stored_overrides_applies_valid_state(remediation):
    errors = reload_stored_overrides(remediation, {"dry_run": True})
    assert errors == []
    assert remediation.dry_run is True


def test_reload_stored_overrides_fails_loud_on_invalid_state(remediation):
    errors = reload_stored_overrides(remediation, {"dry_run": "yes"})
    assert errors != []
    assert remediation.dry_run is False  # untouched


@pytest.mark.asyncio
async def test_a_real_execute_call_reflects_an_applied_dry_run_toggle_immediately(remediation):
    """The end-to-end guarantee this feature depends on: mutating
    RemediationEngine.dry_run live changes what a REAL execute() call does
    on its very next invocation -- no rebuild, no restart. Mirrors
    test_remediation_allowlist.py's own dry-run tests but proves the LIVE
    toggle specifically."""
    from .conftest import make_plan
    from .test_remediation_allowlist import allow
    from app.models.incident import RemediationAction

    plan = make_plan(RemediationAction.RESTART_DEPLOYMENT)
    verdict = allow(RemediationAction.RESTART_DEPLOYMENT)

    before = await remediation.execute(plan, verdict)
    assert before.dry_run is False
    assert before.succeeded is True  # the fake k8s client accepts the real patch

    errors, diffs = validate_changes(remediation, {"dry_run": True})
    apply_diffs(remediation, diffs)

    after = await remediation.execute(plan, verdict)
    assert after.dry_run is True


def test_read_only_summary_exposes_the_real_action_ladder():
    from app.models.incident import RemediationAction, RootCause

    summary = read_only_summary()
    ladder = summary["action_ladder"]
    assert ladder[RootCause.BAD_DEPLOYMENT.value] == [
        RemediationAction.ROLLBACK_DEPLOYMENT.value,
        RemediationAction.RESTART_DEPLOYMENT.value,
    ]
    assert ladder[RootCause.DATABASE_FAILURE.value] == []  # never remediable
    assert "fallback_confidence_discount" in summary
