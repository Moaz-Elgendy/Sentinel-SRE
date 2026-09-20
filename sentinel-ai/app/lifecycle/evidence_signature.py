"""
Evidence signatures — what counts as *materially new* evidence.

Problem this solves
-------------------
An ESCALATED incident is waiting for a human. Alertmanager will keep sending
the same alert (repeat_interval, group changes, retries). If every repeat
restarted the lifecycle we would get the exact loop this module exists to
prevent:

    alert -> RCA -> low confidence -> escalate -> alert -> RCA -> ...

So a repeat must NOT reopen anything unless the *situation* changed. This
module turns an `Evidence` bundle into a small, coarse, deterministic
"signature" and compares two of them. Coarse on purpose: a signature that
changed on every scrape (raw error_rate = 0.4137 vs 0.4141) would call
everything "new" and reopen forever, which is the bug again with extra steps.

What is material (the complete list — documented in docs/incident-engine.md)
---------------------------------------------------------------------------
A change in any of these between the baseline (taken when Sentinel last made
a decision) and fresh evidence:

  * deployment_revision   - a new ReplicaSet revision / image tag / commit
                            (someone deployed something)
  * error_rate_band       - 5xx ratio crossed a band edge (0, 1%, 5%, 25%, 50%)
  * latency_band          - p95 crossed a band edge (0.25s, 1s, 2.5s, 5s)
  * memory_band           - memory DOUBLED (log2 band) — a slow leak creeping
                            does not qualify, a step change does
  * cpu_band              - CPU crossed a band edge (0.25, 0.5, 0.9, 1.5 cores)
  * restart_band          - pod restart total crossed 0 / 1-2 / 3-9 / 10+
  * health                - /readyz status or HTTP code class changed
  * availability          - up{} flipped
  * replica_shortfall     - available vs desired replicas changed state
  * new_warning_event     - a Kubernetes Warning `reason` not in the baseline
  * new_error_pattern     - a normalised log-error template not in the baseline

What is NOT material: raw metric jitter within a band, the same log line
appearing more often, timestamps, pod names, the same events re-listed,
anything that merely shrank away (a Warning event ageing out of the window is
not "new information").

Human actions (an explicit re-run, a temporary authorization, feedback that
this diagnosis was wrong) are handled by the callers, not here: a person
asking Sentinel to look again does not need the evidence to have moved.
"""
from __future__ import annotations

import math
import re
from typing import Any

from app.models.incident import Evidence

# Band edges. Values are upper-exclusive bucket boundaries.
_ERROR_RATE_EDGES = (0.0, 0.01, 0.05, 0.25, 0.50)
_LATENCY_EDGES = (0.25, 1.0, 2.5, 5.0)
_CPU_EDGES = (0.25, 0.5, 0.9, 1.5)
_RESTART_EDGES = (1, 3, 10)

# Strip volatile tokens so "timeout after 3021ms on 10.0.0.7" and
# "timeout after 3187ms on 10.0.0.9" are the same pattern.
_COUNT_SUFFIX = re.compile(r"\s*\(x\d+\)\s*$")
_HEX_LONG = re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_WS = re.compile(r"\s+")

MAX_PATTERNS = 8


def _band(value: float | None, edges: tuple[float, ...]) -> int | None:
    """Which band `value` falls in: the index of the first edge it does not
    exceed, or len(edges) if it exceeds them all.

    None stays None ("could not see it"), which is its own state: going from
    None to a number IS a change worth noticing (a collector recovered).
    """
    if value is None:
        return None
    for i, edge in enumerate(edges):
        if value <= edge:
            return i
    return len(edges)


def _memory_band(memory_bytes: float | None) -> int | None:
    if memory_bytes is None or memory_bytes <= 0:
        return None
    # log2 of MiB: a doubling moves exactly one band.
    return int(math.floor(math.log2(max(memory_bytes / (1024 * 1024), 1.0))))


def _restart_band(total: int) -> int:
    for i, edge in enumerate(_RESTART_EDGES):
        if total < edge:
            return i
    return len(_RESTART_EDGES)


def normalise_log_pattern(message: str) -> str:
    """Reduce a log line to its template."""
    text = _COUNT_SUFFIX.sub("", message or "").strip().lower()
    text = _IP.sub("<ip>", text)
    text = _HEX_LONG.sub("<id>", text)
    text = _NUMBER.sub("<n>", text)
    return _WS.sub(" ", text)[:120]


def _deployment_revision(evidence: Evidence) -> str | None:
    """A string that changes iff a new revision/image was rolled out."""
    parts: list[str] = []
    history = evidence.replicaset_history or []
    if history:
        newest = history[0]
        parts.append(f"rev={newest.get('revision')}")
        images = newest.get("images") or []
        if images:
            parts.append(f"img={images[0]}")
    elif evidence.deployment:
        images = evidence.deployment.get("images") or []
        if images:
            parts.append(f"img={images[0]}")
    commit = (evidence.deploy_commit or {}).get("sha")
    if commit:
        parts.append(f"sha={str(commit)[:12]}")
    return "|".join(parts) or None


def compute_signature(evidence: Evidence | None) -> dict[str, Any] | None:
    """Coarse, JSON-serialisable fingerprint of the SITUATION (not the alert)."""
    if evidence is None:
        return None

    deployment = evidence.deployment or {}
    desired = deployment.get("desired_replicas")
    available = deployment.get("available_replicas")
    if desired is None or available is None:
        replica_state = None
    else:
        replica_state = "short" if available < desired else "ok"

    warning_reasons = sorted(
        {
            str(e.get("reason"))
            for e in (evidence.k8s_events or [])
            if e.get("type") == "Warning" and e.get("reason")
        }
    )

    patterns: list[str] = []
    for message in evidence.log_sample_messages or []:
        pattern = normalise_log_pattern(message)
        if pattern and pattern not in patterns:
            patterns.append(pattern)
    patterns = sorted(patterns)[:MAX_PATTERNS]

    return {
        "deployment_revision": _deployment_revision(evidence),
        "error_rate_band": _band(evidence.error_rate, _ERROR_RATE_EDGES),
        "latency_band": _band(evidence.p95_latency_seconds, _LATENCY_EDGES),
        "memory_band": _memory_band(evidence.memory_bytes),
        "cpu_band": _band(evidence.cpu_cores, _CPU_EDGES),
        "restart_band": _restart_band(evidence.restart_count_total or 0),
        "health": [evidence.health_status, evidence.health_http_code],
        "availability": evidence.up,
        "replica_state": replica_state,
        "warning_event_reasons": warning_reasons,
        "error_patterns": patterns,
    }


# Scalar keys where ANY difference is material, with the wording used in the
# timeline / Sentinel Live.
_SCALAR_LABELS: dict[str, str] = {
    "deployment_revision": "a new deployment/revision appeared",
    "error_rate_band": "the 5xx error rate moved to a different band",
    "latency_band": "p95 latency moved to a different band",
    "memory_band": "memory usage changed by a factor of two or more",
    "cpu_band": "CPU usage moved to a different band",
    "restart_band": "the pod restart count moved to a different band",
    "health": "the health endpoint status changed",
    "availability": "target availability (up) changed",
    "replica_state": "replica availability changed",
}

# Set-valued keys where only ADDITIONS are material.
_SET_LABELS: dict[str, str] = {
    "warning_event_reasons": "new Kubernetes Warning event(s)",
    "error_patterns": "new log error pattern(s)",
}


def material_changes(
    baseline: dict[str, Any] | None, current: dict[str, Any] | None
) -> list[str]:
    """Human-readable reasons `current` is materially different from
    `baseline`; empty list means "the same situation".

    Both None / missing baseline -> [] : with nothing to compare against we
    refuse to invent a change (the caller records `current` as the baseline
    instead). Erring towards "no change" is the safe direction here: the cost
    of a missed reopen is a human notices the alert they already got; the
    cost of a false reopen is the loop.
    """
    if not baseline or not current:
        return []
    reasons: list[str] = []
    for key, label in _SCALAR_LABELS.items():
        if baseline.get(key) != current.get(key):
            reasons.append(f"{label} ({key}: {baseline.get(key)!r} -> {current.get(key)!r})")
    for key, label in _SET_LABELS.items():
        added = sorted(set(current.get(key) or []) - set(baseline.get(key) or []))
        if added:
            shown = ", ".join(str(a) for a in added[:3])
            reasons.append(f"{label}: {shown}")
    return reasons
