"""
OPERATIONAL MEMORY tests (lifecycle/memory.py).
"""
from __future__ import annotations

from app.lifecycle.evidence_signature import compute_signature
from app.lifecycle.memory import (
    MEMORY_MAX_PENALTY_MULTIPLIER,
    SimilarIncident,
    build_similarity_bias,
    find_similar_incidents,
    similarity,
)
from app.models.incident import Evidence


def _signature(**overrides) -> dict:
    return compute_signature(Evidence(**overrides))


def _record(
    incident_id: str,
    *,
    evidence: Evidence,
    created_at: float = 1000.0,
    root_cause: str = "memory_leak",
    action: str | None = "restart_deployment",
    succeeded: bool = True,
    validated: bool = True,
    escalated: bool = False,
) -> dict:
    attempt = None
    if action is not None:
        attempt = {
            "plan": {"action": action, "params": {}},
            "result": {"succeeded": succeeded},
            "validation": {"outcome": "passed" if validated else "failed"},
        }
    return {
        "id": incident_id,
        "created_at": created_at,
        "escalated": escalated,
        "hypothesis": {"root_cause": root_cause},
        "attempts": [attempt] if attempt else [],
        "evidence": evidence.to_dict(),
    }


def test_identical_evidence_has_similarity_one():
    ev = Evidence(error_rate=0.4, p95_latency_seconds=2.0, memory_bytes=500_000_000)
    sig = compute_signature(ev)
    assert similarity(sig, sig) == 1.0


def test_wildly_different_evidence_has_low_similarity():
    healthy = _signature(error_rate=0.0, p95_latency_seconds=0.1, up=1.0)
    on_fire = _signature(
        error_rate=0.9,
        p95_latency_seconds=8.0,
        up=0.0,
        memory_bytes=2_000_000_000,
        restart_count_total=15,
    )
    assert similarity(healthy, on_fire) < 0.5


def test_unset_fields_on_both_sides_are_not_counted_as_agreement():
    """Two incidents that never collected error rate, latency, memory, CPU,
    availability, or replica state must not look similar purely because
    both sides are unset there. `restart_count_total` is the one scalar
    that always has a real value (it defaults to 0, not None), so make it
    the single genuine point of disagreement and confirm that alone drives
    the score to 0 rather than being diluted or ignored.
    """
    a = compute_signature(Evidence(restart_count_total=0))
    b = compute_signature(Evidence(restart_count_total=20))
    assert similarity(a, b) == 0.0


def test_find_similar_incidents_ranks_by_score_and_respects_the_floor():
    current = _signature(error_rate=0.4, p95_latency_seconds=2.0, restart_count_total=5)
    close = _record(
        "INC-CLOSE",
        evidence=Evidence(error_rate=0.42, p95_latency_seconds=2.1, restart_count_total=4),
        root_cause="memory_leak",
    )
    unrelated = _record(
        "INC-UNRELATED",
        evidence=Evidence(error_rate=0.0, p95_latency_seconds=0.05, up=1.0),
        root_cause="capacity_shortfall",
        action="scale_deployment",
    )
    results = find_similar_incidents(current, [close, unrelated], min_similarity=0.5)
    assert [r.incident_id for r in results] == ["INC-CLOSE"]
    assert results[0].root_cause == "memory_leak"
    assert results[0].action_taken == "restart_deployment"
    assert results[0].validated is True


def test_find_similar_incidents_reports_escalated_outcomes_honestly():
    current = _signature(error_rate=0.5, p95_latency_seconds=3.0)
    escalated = _record(
        "INC-ESCALATED",
        evidence=Evidence(error_rate=0.5, p95_latency_seconds=3.0),
        action=None,
        escalated=True,
    )
    results = find_similar_incidents(current, [escalated], min_similarity=0.5)
    assert len(results) == 1
    assert results[0].escalated is True
    assert results[0].action_taken is None
    assert "escalated to a human" in results[0].as_supporting_note()


def test_supporting_note_reports_a_failed_remediation_as_failed_not_resolved():
    """A past incident where the action ran but did not fix the problem must
    never be described as "resolved autonomously" — that would misrepresent
    a failure as a success in the one place a human reads this citation."""
    current = _signature(error_rate=0.5, p95_latency_seconds=3.0)
    failed = _record(
        "INC-FAILED",
        evidence=Evidence(error_rate=0.5, p95_latency_seconds=3.0),
        action="restart_deployment",
        succeeded=False,
        escalated=False,
    )
    results = find_similar_incidents(current, [failed], min_similarity=0.5)
    note = results[0].as_supporting_note()
    assert "did not resolve the incident" in note
    assert "resolved autonomously" not in note


def test_supporting_note_flags_unconfirmed_recovery_distinctly():
    current = _signature(error_rate=0.5, p95_latency_seconds=3.0)
    unconfirmed = _record(
        "INC-UNCONFIRMED",
        evidence=Evidence(error_rate=0.5, p95_latency_seconds=3.0),
        action="restart_deployment",
        succeeded=True,
        validated=False,
        escalated=False,
    )
    results = find_similar_incidents(current, [unconfirmed], min_similarity=0.5)
    note = results[0].as_supporting_note()
    assert "recovery was not confirmed" in note
    assert "resolved autonomously" not in note


def test_supporting_note_for_no_recorded_attempt_is_honest_about_that():
    current = _signature(error_rate=0.5, p95_latency_seconds=3.0)
    no_attempt = _record(
        "INC-NOATTEMPT",
        evidence=Evidence(error_rate=0.5, p95_latency_seconds=3.0),
        action=None,
        escalated=False,
    )
    results = find_similar_incidents(current, [no_attempt], min_similarity=0.5)
    note = results[0].as_supporting_note()
    assert "without an autonomous remediation attempt" in note



def test_no_current_signature_returns_nothing():
    assert find_similar_incidents(None, [_record("INC-X", evidence=Evidence())]) == []


def test_limit_is_respected():
    current = _signature(error_rate=0.4)
    records = [
        _record(f"INC-{i}", evidence=Evidence(error_rate=0.4), created_at=float(i))
        for i in range(10)
    ]
    results = find_similar_incidents(current, records, min_similarity=0.0, limit=3)
    assert len(results) == 3


# ---------------------------------------------------------------------------
# build_similarity_bias() — the second, independent bounded feedback signal
# (see the module docstring; merged with learning.py's bias in
# orchestrator.py via learning.merge_bias()).
# ---------------------------------------------------------------------------
def _similar(action_taken="restart_deployment", succeeded=True, validated=True, score=0.9, incident_id="INC-1"):
    return SimilarIncident(
        incident_id=incident_id,
        occurred_at=0.0,
        similarity_score=score,
        root_cause="memory_leak",
        action_taken=action_taken,
        succeeded=succeeded,
        validated=validated,
        escalated=False,
    )


def test_similarity_bias_below_min_samples_is_no_bias_at_all():
    """A single matching neighbour, however it turned out, must not move a
    decision — one anecdote is not a trend, same principle as learning.py's
    own sample floor."""
    assert build_similarity_bias([_similar(incident_id="INC-1")]) == {}


def test_similarity_bias_two_successes_is_no_penalty():
    similar = [_similar(incident_id="INC-1"), _similar(incident_id="INC-2")]
    assert build_similarity_bias(similar)["restart_deployment"] == 1.0


def test_similarity_bias_two_failures_is_capped_at_the_memory_ceiling():
    similar = [
        _similar(incident_id="INC-1", succeeded=False),
        _similar(incident_id="INC-2", succeeded=False),
    ]
    assert build_similarity_bias(similar)["restart_deployment"] == MEMORY_MAX_PENALTY_MULTIPLIER


def test_similarity_bias_ceiling_is_gentler_than_learnings():
    from app.lifecycle.learning import MAX_PENALTY_MULTIPLIER

    assert MEMORY_MAX_PENALTY_MULTIPLIER > MAX_PENALTY_MULTIPLIER


def test_similarity_bias_unvalidated_success_does_not_count_as_working():
    """succeeded=True but validated=False means the action ran without
    error but recovery was never confirmed — as_supporting_note() already
    treats this as distinct from a real success, and the bias must agree."""
    similar = [
        _similar(incident_id="INC-1", succeeded=True, validated=False),
        _similar(incident_id="INC-2", succeeded=True, validated=False),
    ]
    assert build_similarity_bias(similar)["restart_deployment"] == MEMORY_MAX_PENALTY_MULTIPLIER


def test_similarity_bias_ignores_incidents_with_no_executed_action():
    similar = [
        _similar(incident_id="INC-1", action_taken=None, succeeded=None),
        _similar(incident_id="INC-2", action_taken=None, succeeded=None),
    ]
    assert build_similarity_bias(similar) == {}


def test_similarity_bias_weights_a_closer_match_more_than_a_borderline_one():
    """One close, failed match and one borderline, successful match should
    pull the ratio toward the closer match's outcome, not split it evenly."""
    similar = [
        _similar(incident_id="INC-CLOSE", succeeded=False, score=0.95),
        _similar(incident_id="INC-BORDERLINE", succeeded=True, score=0.61),
    ]
    bias = build_similarity_bias(similar)["restart_deployment"]
    midpoint = MEMORY_MAX_PENALTY_MULTIPLIER + (1.0 - MEMORY_MAX_PENALTY_MULTIPLIER) * 0.5
    assert bias < midpoint  # the closer (failed) match dominates


def test_similarity_bias_never_exceeds_1_0_or_drops_below_its_floor():
    similar = [_similar(incident_id=f"INC-{i}", succeeded=True) for i in range(5)]
    bias = build_similarity_bias(similar)["restart_deployment"]
    assert MEMORY_MAX_PENALTY_MULTIPLIER <= bias <= 1.0


def test_similarity_bias_is_per_action_not_pooled_across_actions():
    similar = [
        _similar(incident_id="INC-1", action_taken="restart_deployment", succeeded=False),
        _similar(incident_id="INC-2", action_taken="restart_deployment", succeeded=False),
        _similar(incident_id="INC-3", action_taken="scale_deployment", succeeded=True),
    ]
    bias = build_similarity_bias(similar)
    assert bias["restart_deployment"] == MEMORY_MAX_PENALTY_MULTIPLIER
    assert "scale_deployment" not in bias  # only one sample for this action
