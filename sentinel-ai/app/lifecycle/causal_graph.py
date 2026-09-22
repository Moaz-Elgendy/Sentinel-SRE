"""
CAUSAL INCIDENT GRAPH — an evidence-backed projection of one incident.

Position in the architecture: this module reads an already-persisted
`Incident` and derives a graph from it. It does not collect anything new,
does not run at investigation time, and is not on the remediation hot path.
It is a *view*, computed on request (GET /api/incidents/{id}/causal-graph),
over data every other module already produces: `Evidence`, `Hypothesis`,
`AttemptRecord`, and `Incident.timeline`.

Why not a graph database: this cluster runs a handful of incidents. A
projection function that a reviewer (or a test) can read top to bottom is
more maintainable than a persisted graph store, an ingestion pipeline for
it, and a query language to keep in sync with the Incident schema. If
incident volume ever made that untrue, this is the single place to replace.

The one rule that matters: every edge is labelled with a `kind` that says
what KIND of link it is, and the kinds are never blurred:

  * "fact"        - directly observed and not in dispute (a commit sha, a
                    replicaset revision, a metric value, a log line existing).
  * "correlation" - two facts co-occurred; this is what `evidence.correlations`
                    (produced by lifecycle/correlation.py) already records.
                    A correlation edge is NOT a claim that one caused the
                    other — see lifecycle/correlation.py's own module
                    docstring, which this graph is not allowed to contradict.
  * "hypothesis"  - Sentinel's belief, with its confidence and *reasoning*
                    (the existing, already-concise `Hypothesis.reasoning` /
                    `llm_note` fields) - never raw LLM chain-of-thought,
                    because those fields never held that in the first place.
  * "action"      - a remediation candidate: proposed, and (if a verdict
                    exists) allowed or denied, with the real reason. Covers
                    both a known RemediationAction the Decision Engine
                    proposed and a Deep Investigation's
                    DeepRemediationProposal (lifecycle/deep_investigation.py)
                    — the same kind, because from this graph's perspective
                    both are "something Sentinel proposed doing about this
                    incident"; `detail.source` says which, and a deep
                    proposal's `detail.status` carries its own richer
                    lifecycle (suggested/authorized/.../rejected) rather than
                    the known-action allowed/denied shape.
  * "outcome"     - what actually happened after an action executed:
                    RemediationResult + ValidationReport, or (for a Deep
                    Investigation proposal) the equivalent DeepRemediationResult
                    + ValidationReport once a human has authorised it.

A GUI (or any other consumer) can therefore render fact edges as solid lines
and correlation/hypothesis edges as visually distinct from them, without this
module having to know anything about rendering.
"""
from __future__ import annotations

from typing import Any

from app.models.incident import DeepProposalStatus, Incident


def _add_node(
    nodes: list[dict[str, Any]],
    seen: set[str],
    node_id: str,
    kind: str,
    label: str,
    *,
    at: float | None = None,
    detail: dict[str, Any] | None = None,
) -> str:
    if node_id not in seen:
        seen.add(node_id)
        nodes.append(
            {
                "id": node_id,
                "kind": kind,
                "label": label,
                "at": at,
                "detail": detail or {},
            }
        )
    return node_id


def _add_edge(
    edges: list[dict[str, Any]],
    source: str,
    target: str,
    kind: str,
    label: str,
    *,
    evidence: list[str] | None = None,
) -> None:
    edges.append(
        {
            "source": source,
            "target": target,
            "kind": kind,
            "label": label,
            "evidence": evidence or [],
        }
    )


def build_causal_graph(incident: Incident) -> dict[str, Any]:
    """Project `incident` into {"nodes": [...], "edges": [...]}.

    Deterministic and side-effect free: same incident record in, same graph
    out. Safe to call on an incident at any lifecycle stage — an incident
    still under investigation simply produces a smaller graph (no action/
    outcome nodes yet), never a fabricated later stage.
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen: set[str] = set()

    ev = incident.evidence
    app = incident.app or "unknown-app"
    namespace = incident.namespace

    alert_id = _add_node(
        nodes,
        seen,
        f"alert:{incident.id}",
        "fact",
        incident.alertname,
        at=incident.created_at,
        detail={
            "severity": incident.severity.value,
            "app": app,
            "namespace": namespace,
            "summary": incident.summary,
            "occurrence": incident.occurrence,
        },
    )

    commit_id: str | None = None
    if ev and ev.deploy_commit and ev.deploy_commit.get("sha"):
        sha = str(ev.deploy_commit["sha"])
        commit_id = _add_node(
            nodes, seen, f"commit:{sha[:12]}", "fact", f"commit {sha[:12]}", detail=ev.deploy_commit
        )

    deployment_id: str | None = None
    if ev and ev.replicaset_history:
        newest = ev.replicaset_history[0]
        rev = newest.get("revision")
        deployment_id = _add_node(
            nodes,
            seen,
            f"deployment:{namespace}/{app}@rev{rev}",
            "fact",
            f"{app} revision {rev}",
            at=newest.get("created_at"),
            detail=newest,
        )
    elif ev and ev.deployment:
        deployment_id = _add_node(
            nodes, seen, f"deployment:{namespace}/{app}", "fact", f"{app} deployment", detail=ev.deployment
        )

    if commit_id and deployment_id:
        _add_edge(
            edges,
            commit_id,
            deployment_id,
            "fact",
            "deployed as",
            evidence=["replicaset created from this commit's image"],
        )

    workload_id: str | None = None
    if ev and (ev.pods or ev.restart_count_total or ev.init_container_logs):
        workload_id = _add_node(
            nodes,
            seen,
            f"workload:{namespace}/{app}/pods",
            "fact",
            f"{app} pods",
            detail={
                "pod_count": len(ev.pods or []),
                "restart_count_total": ev.restart_count_total,
                "init_container_failures": len(ev.init_container_logs or []),
            },
        )
        if deployment_id:
            _add_edge(edges, deployment_id, workload_id, "fact", "manages")

    metrics_id: str | None = None
    if ev is not None:
        metrics_id = _add_node(
            nodes,
            seen,
            "evidence:metrics",
            "fact",
            "metrics",
            at=ev.collected_at,
            detail={
                "error_rate": ev.error_rate,
                "p95_latency_seconds": ev.p95_latency_seconds,
                "cpu_cores": ev.cpu_cores,
                "memory_bytes": ev.memory_bytes,
                "memory_growth_bytes": ev.memory_growth_bytes,
                "up": ev.up,
                "request_rate": ev.request_rate,
            },
        )

    logs_id: str | None = None
    if ev is not None and (ev.log_error_count or ev.log_sample_messages):
        logs_id = _add_node(
            nodes,
            seen,
            "evidence:logs",
            "fact",
            "logs",
            detail={
                "log_error_count": ev.log_error_count,
                "sample_messages": (ev.log_sample_messages or [])[:5],
            },
        )

    health_id: str | None = None
    if ev is not None and (ev.health_status or ev.health_http_code is not None):
        health_id = _add_node(
            nodes,
            seen,
            "evidence:health",
            "fact",
            f"health: {ev.health_status or 'unknown'}",
            detail={"status": ev.health_status, "http_code": ev.health_http_code, "checks": ev.health_checks},
        )

    events_id: str | None = None
    if ev is not None and ev.errors:
        events_id = _add_node(
            nodes,
            seen,
            "evidence:collection_errors",
            "fact",
            "evidence collection errors",
            detail={"errors": ev.errors},
        )

    # Evidence -> alert: what was actually observed while this alert was firing.
    for node_id in (metrics_id, logs_id, health_id, workload_id):
        if node_id:
            _add_edge(edges, node_id, alert_id, "fact", "observed during incident")

    # Correlations: evidence.correlations is already the deterministic,
    # non-causal output of lifecycle/correlation.py. We attach each string as
    # a correlation edge from the evidence it most plausibly concerns to the
    # alert, rather than inventing new correlation logic here.
    correlation_targets = [n for n in (deployment_id, workload_id, metrics_id, logs_id, health_id) if n]
    for i, note in enumerate(ev.correlations if ev else []):
        source = correlation_targets[i % len(correlation_targets)] if correlation_targets else alert_id
        corr_id = _add_node(nodes, seen, f"correlation:{i}", "correlation", note)
        _add_edge(edges, source, corr_id, "correlation", "co-occurred with")
        _add_edge(edges, corr_id, alert_id, "correlation", "noted alongside")

    # Hypothesis: Sentinel's belief. reasoning/llm_note are already the
    # concise, human-facing summaries RCA produces (see rca.py) - never raw
    # model chain-of-thought, because RCA never stores that.
    hypothesis_id: str | None = None
    if incident.hypothesis is not None:
        h = incident.hypothesis
        hypothesis_id = _add_node(
            nodes,
            seen,
            f"hypothesis:{h.root_cause.value}",
            "hypothesis",
            h.root_cause.value.replace("_", " "),
            detail={
                "confidence": h.confidence,
                "reasoning": h.reasoning,
                "recommended_action": h.recommended_action.value,
                "source": h.source,
                "llm_used": h.llm_used,
                "llm_note": h.llm_note,
                "supporting": h.supporting,
            },
        )
        # supporting evidence -> hypothesis, evidenced by the hypothesis's own
        # `supporting` list (already populated by rca.py's rule cascade).
        for target in (metrics_id, logs_id, health_id, workload_id, deployment_id, events_id):
            if target:
                _add_edge(edges, target, hypothesis_id, "hypothesis", "supports", evidence=list(h.supporting))
        _add_edge(edges, alert_id, hypothesis_id, "hypothesis", "explains")

    # Actions and outcomes: one pair of nodes per attempt, in order. Tracks
    # the most recent outcome BY TIME (not just the last attempt in the
    # list), because a Deep Investigation proposal — appended below — can
    # execute after every known-action attempt and become the incident's
    # real final outcome; whichever happened last is what the closing
    # "resolves" / "did not fully resolve" edge must point from.
    previous_outcome_id: str | None = None
    last_outcome_at: float | None = None
    for i, attempt in enumerate(incident.attempts):
        plan = attempt.plan
        action_id = _add_node(
            nodes,
            seen,
            f"action:{i}",
            "action",
            plan.action.value.replace("_", " "),
            at=attempt.at,
            detail={
                "source": "known_action",
                "confidence": plan.confidence,
                "rationale": plan.rationale,
                "namespace": plan.params.namespace,
                "deployment": plan.params.deployment,
                "allowed": attempt.verdict.allowed if attempt.verdict else None,
                "denial_reason": (
                    attempt.verdict.reason.value
                    if attempt.verdict and attempt.verdict.reason
                    else None
                ),
                "denial_detail": attempt.verdict.detail if attempt.verdict else "",
                "risk": attempt.verdict.risk if attempt.verdict else None,
            },
        )
        if hypothesis_id:
            _add_edge(edges, hypothesis_id, action_id, "hypothesis", "recommended")

        if attempt.result is None:
            # Policy denied it before execution; no outcome node, because
            # nothing happened to have an outcome.
            continue

        outcome_id = _add_node(
            nodes,
            seen,
            f"outcome:{i}",
            "outcome",
            "recovered" if (attempt.validation and attempt.validation.outcome.value == "passed") else "outcome",
            at=attempt.result.started_at,
            detail={
                "succeeded": attempt.result.succeeded,
                "dry_run": attempt.result.dry_run,
                "result_detail": attempt.result.detail,
                "validation_outcome": attempt.validation.outcome.value if attempt.validation else None,
                "validation_detail": attempt.validation.detail if attempt.validation else None,
                "failed_checks": attempt.validation.failed_checks if attempt.validation else [],
            },
        )
        _add_edge(edges, action_id, outcome_id, "action", "executed")
        if last_outcome_at is None or attempt.result.started_at >= last_outcome_at:
            previous_outcome_id = outcome_id
            last_outcome_at = attempt.result.started_at

    # Deep Investigation proposals: the same action/outcome shape as above,
    # reusing this graph rather than a parallel view (see this module's own
    # "no parallel systems" design note). A proposal is generated only once
    # known remediation was insufficient (lifecycle/deep_investigation.py),
    # so it hangs off the same hypothesis node, not off the last known
    # action — a deep proposal is a response to the diagnosis being
    # insufficient to act on safely, not a continuation of one specific
    # rejected attempt.
    for proposal in incident.deep_proposals:
        target = proposal.target
        deep_action_id = _add_node(
            nodes,
            seen,
            f"deep_action:{proposal.id}",
            "action",
            proposal.action_type.value.replace("_", " "),
            at=proposal.created_at,
            detail={
                "source": "deep_investigation",
                "status": proposal.status.value,
                "confidence": proposal.confidence,
                "risk_level": proposal.risk_level,
                "reason": proposal.reason,
                "namespace": target.namespace,
                "deployment": target.deployment,
                "container": target.container,
                "env_var": target.key,
                "rejected_reason": proposal.rejected_reason or "",
            },
        )
        if hypothesis_id:
            _add_edge(
                edges, hypothesis_id, deep_action_id, "hypothesis",
                "insufficient for known remediation; deep investigation proposed",
            )

        if proposal.status not in (
            DeepProposalStatus.EXECUTED,
            DeepProposalStatus.VALIDATED,
            DeepProposalStatus.FAILED,
        ):
            # Still suggested (or authorized/executing at the moment of this
            # snapshot), or rejected by policy before ever touching the
            # cluster — either way, nothing happened yet to have an outcome.
            continue

        deep_outcome_id = _add_node(
            nodes,
            seen,
            f"deep_outcome:{proposal.id}",
            "outcome",
            "recovered" if proposal.status == DeepProposalStatus.VALIDATED else "outcome",
            # DeepRemediationProposal has no separate "executed at" field —
            # created_at is the closest available timestamp, and since this
            # is a single, short-lived, human-authorized action (see
            # AUTHORIZATION_TTL_SECONDS in routers/authorizations.py) it is
            # never far from when execution actually happened.
            at=proposal.created_at,
            detail={
                "result_detail": proposal.result_detail,
            },
        )
        _add_edge(edges, deep_action_id, deep_outcome_id, "action", "executed")
        if last_outcome_at is None or proposal.created_at >= last_outcome_at:
            previous_outcome_id = deep_outcome_id
            last_outcome_at = proposal.created_at

    if previous_outcome_id:
        _add_edge(
            edges,
            previous_outcome_id,
            alert_id,
            "outcome",
            "resolves" if incident.status.value in ("resolved", "auto_resolved") else "did not fully resolve",
        )

    return {"incident_id": incident.id, "nodes": nodes, "edges": edges}
