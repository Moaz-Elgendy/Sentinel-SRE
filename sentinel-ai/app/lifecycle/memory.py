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

### What this module does NOT do

* It does not touch confidence, root cause, or the candidate action list.
  Its only output (`hypothesis.supporting` entries, via the orchestrator)
  is prose alongside evidence Sentinel already collected — informational,
  the same way `evidence.correlations` is informational. See
  learning.py's own module docstring for the sibling mechanism (outcome
  bias) and why it is capped so it can only make Sentinel more cautious,
  never less: this module carries that same rule by construction, because
  it never produces a number that reaches the Decision or Policy Engine at
  all — only strings for a human (or the GUI) to read.
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
