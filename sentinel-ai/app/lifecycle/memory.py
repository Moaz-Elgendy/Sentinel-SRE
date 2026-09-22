"""
OPERATIONAL MEMORY — "has Sentinel seen something like this before?"

Reuses `lifecycle/evidence_signature.py`'s `compute_signature()` as the
incident's fingerprint, rather than inventing a second, different notion of
"the same situation". That function was built for a different purpose
(deciding whether a repeat alert on an ESCALATED incident is a materially
new situation), but the fingerprint itself — coarse bands of error rate,
latency, memory, CPU, restarts, health, availability, replica state, plus
normalised log/event patterns — is exactly what "recognise a similar
incident" needs too: coarse on purpose, so two incidents with the same kind
of problem match even though their raw numbers never repeat exactly.

### What this module does

Besides the citations described above, `find_similar_incidents()`'s results
also feed `build_similarity_bias()`, a second, independent bounded feedback
signal alongside learning.py's root-cause-wide outcome bias — see that
function's own docstring for exactly how, and why it is bounded the same
way learning.py's `build_bias()` is (multipliers in
`[MEMORY_MAX_PENALTY_MULTIPLIER, 1.0]`, never above 1.0, so this can only
ever make Sentinel more cautious about an action, never less). The
orchestrator multiplies this bias together with learning.py's before
passing the combined result to `DecisionEngine.candidates()` — two
independent, equally-capped signals, not a second unbounded path to the
same effect.

This is a deliberate change from this module's earlier, citation-only
design: a handful of similarity-matched neighbours is a real (if smaller
and noisier) signal about whether an action worked for a situation like
this one, not just root cause and action in the abstract, and there is no
reason to compute it and then discard it. It remains fundamentally
different from learning.py's signal, and is combined with (never replaces)
it: learning.py aggregates every recorded outcome for a root cause, however
long ago or however differently the incident looked; this module only ever
looks at the handful of incidents whose full evidence signature actually
resembles this one.

### What this module still does NOT do

* It does not choose, invent, or add an action. Both the citations and the
  bias are strictly about actions the static `ACTION_LADDER` (decision.py)
  already offers; a similarity match can make one of those less likely to
  be tried, never more likely to exist.
* It does not persist a second copy of past incidents. Signatures for past
  incidents are recomputed at read time from their stored Evidence
  (SQLiteStore.list_terminal_incidents_for_app), the same way
  lifecycle/causal_graph.py derives its view from the stored record rather
  than a separate store. With this cluster's incident volume (see
  learning.py's own note on the same point), recomputing a handful of
  signatures per lookup is cheap, and it means there is only ever one
  definition of "the signature", never two that can drift.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.lifecycle.evidence_signature import compute_signature
from app.models.incident import Evidence

# Scalar band fields compared for exact agreement. `deployment_revision` is
# deliberately excluded: it identifies WHICH deploy happened, not what KIND
# of failure this is, so two unrelated incidents on two different revisions
# would never match on it even when the failure pattern is identical.
_SCALAR_KEYS = (
    "error_rate_band",
    "latency_band",
    "memory_band",
    "cpu_band",
    "restart_band",
    "health",
    "availability",
    "replica_state",
)
_SET_KEYS = ("warning_event_reasons", "error_patterns")

DEFAULT_MIN_SIMILARITY = 0.6
DEFAULT_LIMIT = 3


def _unset(value: Any) -> bool:
    """True if `value` represents "not observed" rather than a real band.

    Most scalar signature fields are `None` when unobserved. `health` is the
    one exception — evidence_signature.compute_signature() always stores it
    as `[health_status, health_http_code]`, a two-element list that is
    `[None, None]` rather than `None` itself when nothing was observed. This
    normalises both shapes to the same "unset" meaning.
    """
    if value is None:
        return True
    if isinstance(value, list):
        return all(v is None for v in value)
    return False


def similarity(a: dict[str, Any] | None, b: dict[str, Any] | None) -> float:
    """0.0-1.0: fraction of comparable dimensions the two signatures agree on.

    A dimension is "comparable" only when at least one side has a real value
    (both unset means neither incident had that kind of evidence at all,
    which is not agreement — it is absence, and counting it as a match would
    inflate the score for two incidents that simply collected less data).
    """
    if not a or not b:
        return 0.0
    matched = 0.0
    comparable = 0
    for key in _SCALAR_KEYS:
        av, bv = a.get(key), b.get(key)
        if _unset(av) and _unset(bv):
            continue
        comparable += 1
        if av == bv:
            matched += 1.0
    for key in _SET_KEYS:
        aset, bset = set(a.get(key) or []), set(b.get(key) or [])
        if not aset and not bset:
            continue
        comparable += 1
        union = aset | bset
        if union:
            matched += len(aset & bset) / len(union)
    return matched / comparable if comparable else 0.0


@dataclass
class SimilarIncident:
    incident_id: str
    occurred_at: float
    similarity_score: float
    root_cause: str | None
    action_taken: str | None
    succeeded: bool | None
    validated: bool | None
    escalated: bool

    def as_supporting_note(self) -> str:
        """One line for `Hypothesis.supporting` — a human-readable citation,
        never a claim that this incident determined the current diagnosis.

        Distinguishes escalation / failure / unconfirmed-recovery / success
        outcomes rather than collapsing everything non-escalated into
        "resolved autonomously" — the brief asks for "previous remediation
        succeeded/failed" to be surfaced, and a citation that calls a failed
        attempt a success would be actively misleading, not just imprecise.
        """
        if self.escalated:
            outcome = "was escalated to a human"
        elif self.action_taken is None:
            outcome = "resolved without an autonomous remediation attempt"
        elif self.succeeded is False:
            outcome = f"attempted {self.action_taken} but it did not resolve the incident"
        elif self.validated is False:
            outcome = f"applied {self.action_taken}, but recovery was not confirmed"
        else:
            outcome = f"resolved autonomously via {self.action_taken}"
        return (
            f"similar past incident {self.incident_id} "
            f"(similarity {self.similarity_score:.2f}, root cause "
            f"{self.root_cause or 'unknown'}) {outcome}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "occurred_at": self.occurred_at,
            "similarity_score": round(self.similarity_score, 3),
            "root_cause": self.root_cause,
            "action_taken": self.action_taken,
            "succeeded": self.succeeded,
            "validated": self.validated,
            "escalated": self.escalated,
        }


# Below this many similarity-matched incidents that both tried the SAME
# action AND have a definitive outcome, no bias is applied for that action.
# `find_similar_incidents()` surfaces at most DEFAULT_LIMIT (3) neighbours
# per lookup, so this floor means the bias only ever engages when at least
# two of those few neighbours agree on having tried the same thing — a
# single anecdote must never move a decision.
MIN_SIMILAR_SAMPLES_FOR_BIAS = 2

# Gentler ceiling than learning.py's MAX_PENALTY_MULTIPLIER (0.85): this
# signal comes from a handful of similarity-matched neighbours, not a
# root-cause-wide outcome tally built from every incident ever recorded, so
# it is deliberately allowed to move confidence less. Never above 1.0 — see
# the module docstring for why that ceiling is the whole point, same as
# learning.py's.
MEMORY_MAX_PENALTY_MULTIPLIER = 0.92


def build_similarity_bias(similar: list[SimilarIncident]) -> dict[str, float]:
    """Turn similarity-matched past incidents into {action_name: multiplier}.

    Mirrors learning.py's `build_bias()` in spirit and in its safety
    invariant: multipliers are always in `[MEMORY_MAX_PENALTY_MULTIPLIER,
    1.0]`, NEVER above 1.0, so this can only ever make Sentinel more
    cautious about an action, never more confident than the rule-based
    hypothesis already made it. See that function's docstring for the
    argument against ever lifting a ceiling like this.

    Each similar incident with a recorded `action_taken` and a definitive
    outcome (`succeeded` is not `None`) casts one vote, weighted by its own
    `similarity_score`, for whether that action worked here — a close match
    counts for more than a borderline one. An incident with no executed
    action, or whose last attempt has no result recorded at all, casts no
    vote: an absence of information must never bias a decision either way.
    "Worked" requires both a successful result AND (when validation ran) a
    passed validation — an action that "succeeded" but was never confirmed
    to have actually resolved anything must not read as a success story.

    `MIN_SIMILAR_SAMPLES_FOR_BIAS` gates each action independently: a root
    cause with three similar incidents, two of which tried
    `restart_deployment` and one of which tried `rollback_deployment`, only
    ever produces a bias for `restart_deployment`.
    """
    weighted_success: dict[str, float] = {}
    weighted_total: dict[str, float] = {}
    sample_count: dict[str, int] = {}
    for s in similar:
        if not s.action_taken or s.succeeded is None:
            continue
        weight = max(s.similarity_score, 0.0)
        if weight <= 0.0:
            continue
        worked = bool(s.succeeded) and s.validated is not False
        weighted_success[s.action_taken] = weighted_success.get(s.action_taken, 0.0) + (weight if worked else 0.0)
        weighted_total[s.action_taken] = weighted_total.get(s.action_taken, 0.0) + weight
        sample_count[s.action_taken] = sample_count.get(s.action_taken, 0) + 1

    bias: dict[str, float] = {}
    for action, total in weighted_total.items():
        if sample_count[action] < MIN_SIMILAR_SAMPLES_FOR_BIAS or total <= 0.0:
            continue
        success_ratio = weighted_success[action] / total
        multiplier = MEMORY_MAX_PENALTY_MULTIPLIER + (1.0 - MEMORY_MAX_PENALTY_MULTIPLIER) * success_ratio
        # Belt and braces, same as learning.py: clamp so nothing above can
        # ever produce a multiplier outside the documented bound.
        bias[action] = max(MEMORY_MAX_PENALTY_MULTIPLIER, min(1.0, multiplier))
    return bias


def _last_executed_attempt(record: dict[str, Any]) -> dict[str, Any] | None:
    for attempt in reversed(record.get("attempts") or []):
        if attempt.get("result") is not None:
            return attempt
    return None


def find_similar_incidents(
    current_signature: dict[str, Any] | None,
    past_records: list[dict[str, Any]],
    *,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    limit: int = DEFAULT_LIMIT,
) -> list[SimilarIncident]:
    """Rank `past_records` (raw stored incident dicts, already filtered to
    the same app and terminal statuses — see SQLiteStore.
    list_terminal_incidents_for_app) by signature similarity to
    `current_signature`. Returns at most `limit`, highest similarity first,
    dropping anything below `min_similarity`.
    """
    if not current_signature:
        return []

    scored: list[SimilarIncident] = []
    for record in past_records:
        past_evidence = Evidence.from_dict(record.get("evidence"))
        past_signature = compute_signature(past_evidence)
        score = similarity(current_signature, past_signature)
        if score < min_similarity:
            continue
        hypothesis = record.get("hypothesis") or {}
        last_attempt = _last_executed_attempt(record)
        result = (last_attempt or {}).get("result") or {}
        validation = (last_attempt or {}).get("validation") or {}
        plan = (last_attempt or {}).get("plan") or {}
        scored.append(
            SimilarIncident(
                incident_id=record["id"],
                occurred_at=record.get("created_at", 0.0),
                similarity_score=score,
                root_cause=hypothesis.get("root_cause"),
                action_taken=plan.get("action"),
                succeeded=result.get("succeeded") if last_attempt else None,
                validated=(validation.get("outcome") == "passed") if last_attempt else None,
                escalated=bool(record.get("escalated")),
            )
        )

    scored.sort(key=lambda s: s.similarity_score, reverse=True)
    return scored[:limit]
