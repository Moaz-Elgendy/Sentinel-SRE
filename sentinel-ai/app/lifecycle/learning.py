"""
LEARNING — record outcomes and bias future decisions.

What this does: after every incident, write one row per executed action
recording (root cause, action, did it succeed, did recovery validation pass).
Before the next incident's decision, read those rows and produce a small
multiplier per action that nudges the Decision Engine's candidate ordering.

### What this deliberately does NOT do

* **It cannot unlock an action.** The bias is a multiplier on *confidence*,
  and confidence is compared against the Policy Engine's thresholds. A bias
  can lower confidence below a threshold (making Sentinel more cautious) but
  the multiplier is capped at 1.0, so it can never lift a hypothesis over a
  gate it would otherwise fail. Learning can only ever make Sentinel *more*
  conservative or reorder equally-permitted candidates.

  This is the important design decision in this file. A learning system that
  can raise its own confidence will, given a run of luck, eventually
  authorise a rollback it should not have. Capping at 1.0 makes that
  structurally impossible.

* **It cannot add an action to the ladder.** The candidate set comes from the
  static `ACTION_LADDER` table. Learning reorders; it does not invent.

* **It is not a model.** It is a success ratio with a minimum sample size.
  With the volume of incidents this cluster will ever see, anything more
  sophisticated would be fitting noise, and honest simplicity beats a
  gradient nobody can audit.
"""
from __future__ import annotations

import logging

from app.models.incident import Incident, RootCause

logger = logging.getLogger(__name__)

# Below this many recorded attempts we apply no bias at all. Two data points
# are an anecdote, not a trend, and letting them move behaviour would make
# Sentinel's decisions unreproducible for no benefit.
MIN_SAMPLES_FOR_BIAS = 3

# The most learning may penalise a candidate. 0.85 is enough to reorder
# candidates and enough to drop a marginal hypothesis below a threshold, but
# it cannot silently disable an action forever.
MAX_PENALTY_MULTIPLIER = 0.85


def build_bias(stats: dict[str, dict[str, int]]) -> dict[str, float]:
    """Turn historical outcomes into {action_name: multiplier}.

    `stats` is what SQLiteStore.action_stats() returns:
    ``{action: {"attempts": n, "validated": m}}``.

    Multipliers are in [MAX_PENALTY_MULTIPLIER, 1.0]. **Never above 1.0** —
    see the module docstring for why that ceiling is the whole point.
    """
    bias: dict[str, float] = {}
    for action, counts in stats.items():
        attempts = counts.get("attempts", 0)
        validated = counts.get("validated", 0)
        if attempts < MIN_SAMPLES_FOR_BIAS:
            continue
        success_ratio = validated / attempts
        # Linear interpolation: a 100% success history gets 1.0 (no change),
        # a 0% success history gets the maximum penalty.
        multiplier = MAX_PENALTY_MULTIPLIER + (1.0 - MAX_PENALTY_MULTIPLIER) * success_ratio
        # Belt and braces: clamp, so a corrupted row (validated > attempts)
        # cannot produce a multiplier above 1.0.
        bias[action] = max(MAX_PENALTY_MULTIPLIER, min(1.0, multiplier))
    return bias


def record_incident_outcomes(incident: Incident, store) -> list[dict[str, object]]:
    """Persist one outcome row per executed action.

    Returns the rows written, for the incident timeline. Policy-denied
    candidates are not recorded: they never touched the cluster, so they carry
    no information about whether the action works.
    """
    root_cause = (
        incident.hypothesis.root_cause.value
        if incident.hypothesis
        else RootCause.UNKNOWN.value
    )
    written: list[dict[str, object]] = []
    for attempt in incident.attempts:
        if attempt.result is None:
            continue
        if attempt.result.dry_run:
            # A dry run proves nothing about whether the action works, so it
            # must not train the bias. Recording it would teach Sentinel that
            # every action always succeeds.
            continue
        validated = bool(attempt.validation and attempt.validation.passed)
        target = (
            f"{attempt.plan.params.namespace}/{attempt.plan.params.deployment}"
        )
        try:
            store.record_outcome(
                incident_id=incident.id,
                root_cause=root_cause,
                action=attempt.plan.action.value,
                target=target,
                succeeded=attempt.result.succeeded,
                validated=validated,
            )
        except Exception as exc:  # noqa: BLE001
            # A learning write failing must never fail the incident.
            logger.warning(
                "learning_write_failed", extra={"error_detail": str(exc)[:200]}
            )
            continue
        written.append(
            {
                "action": attempt.plan.action.value,
                "succeeded": attempt.result.succeeded,
                "validated": validated,
            }
        )

    logger.info(
        "learning_recorded",
        extra={"root_cause": root_cause, "rows": len(written)},
    )
    return written


def load_bias(root_cause: RootCause, store) -> dict[str, float]:
    """Read the bias for a root cause. Empty dict on any failure."""
    try:
        stats = store.action_stats(root_cause.value)
    except Exception as exc:  # noqa: BLE001
        logger.warning("learning_read_failed", extra={"error_detail": str(exc)[:200]})
        return {}
    bias = build_bias(stats)
    if bias:
        logger.info(
            "learning_bias_applied",
            extra={"root_cause": root_cause.value, "bias": bias},
        )
    return bias
