"""
LEARNING tests (lifecycle/learning.py): the outcome-bias builder and the
merge helper that combines it with memory.py's similarity bias.
"""
from __future__ import annotations

from app.lifecycle.learning import (
    MAX_PENALTY_MULTIPLIER,
    build_bias,
    merge_bias,
)


def test_build_bias_below_min_samples_is_no_bias_at_all():
    """Two attempts, however the outcome split, must not move behaviour —
    two data points are an anecdote, not a trend (see the module docstring)."""
    stats = {"restart_deployment": {"attempts": 2, "validated": 0}}
    assert build_bias(stats) == {}


def test_build_bias_perfect_history_is_no_penalty():
    stats = {"restart_deployment": {"attempts": 5, "validated": 5}}
    assert build_bias(stats)["restart_deployment"] == 1.0


def test_build_bias_all_failures_is_capped_at_the_maximum_penalty():
    stats = {"restart_deployment": {"attempts": 5, "validated": 0}}
    assert build_bias(stats)["restart_deployment"] == MAX_PENALTY_MULTIPLIER


def test_build_bias_partial_success_interpolates_between_the_two():
    stats = {"restart_deployment": {"attempts": 4, "validated": 2}}  # 50%
    bias = build_bias(stats)["restart_deployment"]
    expected = MAX_PENALTY_MULTIPLIER + (1.0 - MAX_PENALTY_MULTIPLIER) * 0.5
    assert bias == expected
    assert MAX_PENALTY_MULTIPLIER < bias < 1.0


def test_build_bias_never_exceeds_1_0_even_with_a_corrupted_row():
    """Belt and braces: validated > attempts should never happen, but if it
    does, the ceiling must still hold."""
    stats = {"restart_deployment": {"attempts": 3, "validated": 9}}
    assert build_bias(stats)["restart_deployment"] == 1.0


def test_build_bias_ignores_actions_below_the_sample_floor_individually():
    stats = {
        "restart_deployment": {"attempts": 5, "validated": 0},
        "scale_deployment": {"attempts": 1, "validated": 1},
    }
    bias = build_bias(stats)
    assert bias == {"restart_deployment": MAX_PENALTY_MULTIPLIER}


def test_merge_bias_multiplies_independent_sources():
    learning_bias = {"restart_deployment": 0.9}
    memory_bias = {"restart_deployment": 0.92}
    merged = merge_bias(learning_bias, memory_bias)
    assert merged["restart_deployment"] == 0.9 * 0.92


def test_merge_bias_treats_an_action_missing_from_one_source_as_neutral():
    """An action learning.py has an opinion on but memory.py has never seen
    must not be penalised by memory's silence — absence is neutral (1.0),
    never a penalty."""
    learning_bias = {"restart_deployment": 0.85}
    memory_bias: dict[str, float] = {}
    merged = merge_bias(learning_bias, memory_bias)
    assert merged["restart_deployment"] == 0.85


def test_merge_bias_combines_actions_present_in_only_one_source():
    learning_bias = {"restart_deployment": 0.9}
    memory_bias = {"scale_deployment": 0.95}
    merged = merge_bias(learning_bias, memory_bias)
    assert merged == {"restart_deployment": 0.9, "scale_deployment": 0.95}


def test_merge_bias_with_no_sources_is_empty():
    assert merge_bias() == {}


def test_merge_bias_never_produces_a_multiplier_above_1_0():
    """Both inputs are already bounded by construction, but this asserts the
    product-of-bounded-factors invariant directly rather than trusting it."""
    merged = merge_bias({"restart_deployment": 1.0}, {"restart_deployment": 1.0})
    assert merged["restart_deployment"] == 1.0
