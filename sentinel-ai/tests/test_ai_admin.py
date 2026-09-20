"""Tests for app/lifecycle/ai_admin.py."""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.lifecycle.ai_admin import (
    EDITABLE_FIELDS,
    apply_diffs,
    read_only_summary,
    rebuild_reasoner,
    reload_stored_overrides,
    snapshot,
    validate_changes,
)


@pytest.fixture
def settings():
    return Settings(_env_file=None)


class _FakeCtx:
    """Minimal stand-in for SentinelContext — only `settings` and
    `reasoner` are touched by rebuild_reasoner/reload_stored_overrides."""

    def __init__(self, settings_obj):
        self.settings = settings_obj
        self.reasoner = None


def test_editable_fields_never_include_api_keys():
    assert "openai_api_key" not in EDITABLE_FIELDS
    assert "gemini_api_key" not in EDITABLE_FIELDS
    assert set(EDITABLE_FIELDS) == {
        "llm_provider",
        "openai_model",
        "openai_timeout_seconds",
        "openai_base_url",
        "gemini_model",
        "gemini_timeout_seconds",
        # Groq is a first-class provider behind the same Reasoner abstraction.
        # Only model/timeout are editable; groq_api_key must never appear.
        "groq_model",
        "groq_timeout_seconds",
    }


def test_snapshot_never_includes_api_keys(settings):
    assert "openai_api_key" not in snapshot(settings)
    assert "gemini_api_key" not in snapshot(settings)


def test_read_only_summary_never_includes_api_keys_only_configured_booleans(settings):
    summary = read_only_summary(settings)
    assert "openai_api_key" not in summary
    assert "gemini_api_key" not in summary
    assert summary["openai_api_key_configured"] is False
    assert summary["gemini_api_key_configured"] is False
    assert summary["temperature"] == 0.0


def test_read_only_summary_reflects_a_configured_key(settings):
    settings.gemini_api_key = "some-key"
    summary = read_only_summary(settings)
    assert summary["gemini_api_key_configured"] is True
    assert summary["openai_api_key_configured"] is False


def test_attempting_to_set_openai_api_key_is_rejected_with_a_clear_reason(settings):
    errors, diffs = validate_changes(settings, {"openai_api_key": "sk-whatever"})
    assert diffs == []
    assert len(errors) == 1
    assert "never be read or written" in errors[0]


def test_attempting_to_set_gemini_api_key_is_rejected(settings):
    errors, diffs = validate_changes(settings, {"gemini_api_key": "whatever"})
    assert diffs == []
    assert "never be read or written" in errors[0]


def test_unknown_field_is_rejected(settings):
    errors, diffs = validate_changes(settings, {"temperature": 0.5})
    assert diffs == []
    assert "not a configurable" in errors[0]


def test_switching_provider_with_no_key_configured_warns_not_errors(settings):
    errors, diffs = validate_changes(settings, {"llm_provider": "gemini"})
    assert errors == []
    assert diffs[0].old_value == "openai"
    assert diffs[0].new_value == "gemini"
    assert "GEMINI_API_KEY" in diffs[0].warning
    assert "rule-based-only" in diffs[0].warning


def test_switching_provider_with_a_key_already_configured_has_no_warning(settings):
    settings.gemini_api_key = "already-set"
    errors, diffs = validate_changes(settings, {"llm_provider": "gemini"})
    assert errors == []
    assert diffs[0].warning is None


def test_invalid_provider_value_is_rejected(settings):
    errors, diffs = validate_changes(settings, {"llm_provider": "claude"})
    assert diffs == []
    assert "must be one of" in errors[0]


def test_empty_model_name_is_rejected(settings):
    errors, diffs = validate_changes(settings, {"openai_model": ""})
    assert diffs == []
    assert "cannot be empty" in errors[0]


def test_non_string_model_is_rejected(settings):
    errors, diffs = validate_changes(settings, {"openai_model": 123})
    assert diffs == []
    assert "must be a string" in errors[0]


def test_empty_base_url_is_valid_and_means_default_provider_endpoint(settings):
    settings.openai_base_url = "https://api.groq.com/openai/v1"
    errors, diffs = validate_changes(settings, {"openai_base_url": ""})
    assert errors == []
    assert diffs[0].new_value == ""


def test_timeout_within_bounds_is_accepted(settings):
    errors, diffs = validate_changes(settings, {"openai_timeout_seconds": 45})
    assert errors == []
    assert diffs[0].new_value == 45.0


def test_timeout_below_minimum_is_rejected(settings):
    errors, diffs = validate_changes(settings, {"gemini_timeout_seconds": 0})
    assert diffs == []
    assert "between 1.0 and 120.0" in errors[0]


def test_timeout_above_maximum_is_rejected(settings):
    errors, diffs = validate_changes(settings, {"openai_timeout_seconds": 121})
    assert diffs == []
    assert "between 1.0 and 120.0" in errors[0]


def test_bool_timeout_is_rejected_even_though_bool_is_an_int_subclass(settings):
    errors, diffs = validate_changes(settings, {"openai_timeout_seconds": True})
    assert diffs == []
    assert "must be a number" in errors[0]


def test_apply_diffs_mutates_the_live_settings(settings):
    errors, diffs = validate_changes(settings, {"openai_model": "gpt-4o"})
    apply_diffs(settings, diffs)
    assert settings.openai_model == "gpt-4o"


def test_rebuild_reasoner_reconstructs_from_updated_settings(settings):
    """The end-to-end guarantee this category depends on: after apply_diffs
    changes provider/model/etc, rebuild_reasoner must produce a NEW Reasoner
    reflecting the change, not silently keep the old one — mirrors
    test_remediation_admin's live-toggle proof for this category's own
    live-reconstruction problem."""
    settings.openai_api_key = "test-key"
    ctx = _FakeCtx(settings)
    rebuild_reasoner(ctx, [])
    assert ctx.reasoner is not None
    assert ctx.reasoner.label.startswith("openai")

    errors, diffs = validate_changes(settings, {"openai_model": "gpt-4o"})
    apply_diffs(settings, diffs)
    rebuild_reasoner(ctx, diffs)
    assert "gpt-4o" in ctx.reasoner.label


def test_rebuild_reasoner_yields_none_when_no_key_is_configured(settings):
    ctx = _FakeCtx(settings)
    rebuild_reasoner(ctx, [])
    assert ctx.reasoner is None


def test_reload_stored_overrides_applies_valid_state_and_rebuilds_reasoner(settings):
    settings.openai_api_key = "test-key"
    ctx = _FakeCtx(settings)
    errors = reload_stored_overrides(ctx, {"openai_model": "gpt-4o"})
    assert errors == []
    assert settings.openai_model == "gpt-4o"
    assert ctx.reasoner is not None
    assert "gpt-4o" in ctx.reasoner.label


def test_reload_stored_overrides_fails_loud_on_invalid_state(settings):
    ctx = _FakeCtx(settings)
    errors = reload_stored_overrides(ctx, {"openai_timeout_seconds": 999})
    assert errors != []
    assert settings.openai_timeout_seconds == 20.0  # untouched
    assert ctx.reasoner is None  # never rebuilt


def test_reload_stored_overrides_is_a_no_op_for_empty_overrides(settings):
    ctx = _FakeCtx(settings)
    errors = reload_stored_overrides(ctx, {})
    assert errors == []
    assert ctx.reasoner is None
