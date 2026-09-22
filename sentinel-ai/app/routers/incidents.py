"""
Read-only incident API.

  GET /api/incidents            — list, newest first
  GET /api/incidents/{id}       — one incident, full record
  GET /api/incidents/{id}/document — the rendered markdown post-mortem
  GET /api/incidents/{id}/causal-graph — evidence-backed graph view (see
                                          lifecycle/causal_graph.py)
  GET /api/incidents/{id}/replay       — what current RCA/policy would
                                          decide from recorded evidence (see
                                          lifecycle/replay.py) — never a live
                                          re-investigation, never an execution

Read-only on purpose. There is no endpoint here that triggers a remediation,
approves one, or edits an incident. The only way an action happens is: an
alert arrives, the lifecycle runs, and the Policy Engine authorises it. Adding
a "retry this action" endpoint would create a second path to the Remediation
Engine that does not go through detection and RCA, and that path would be the
weakest link in the whole design.

The list response omits the evidence bundle, which can be large (log samples,
pod lists, event lists). Fetch a single incident to get it.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.core.deps import get_current_admin
from app.lifecycle.causal_graph import build_causal_graph
from app.lifecycle.replay import replay_incident
from app.models.incident import Incident

router = APIRouter(
    prefix="/api/incidents",
    tags=["incidents"],
    dependencies=[Depends(get_current_admin)],
)


@router.get("")
def list_incidents(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    incident_status: str | None = Query(default=None, alias="status"),
) -> dict[str, Any]:
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="incident store is not ready",
        )
    records = store.list_incidents(limit=limit, offset=offset, status=incident_status)
    # Strip the evidence bundle and the rendered document from list results.
    # Both are large and neither is useful in a list view.
    summaries = []
    for record in records:
        trimmed = {k: v for k, v in record.items() if k not in ("evidence", "documentation")}
        summaries.append(trimmed)
    return {"count": len(summaries), "limit": limit, "offset": offset, "incidents": summaries}


@router.get("/{incident_id}")
def get_incident(incident_id: str, request: Request) -> dict[str, Any]:
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="incident store is not ready",
        )
    record = store.get_incident(incident_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="incident not found"
        )
    return record


@router.get("/{incident_id}/document")
def get_incident_document(incident_id: str, request: Request) -> Response:
    """The markdown post-mortem, as text/markdown.

    Served as raw markdown rather than JSON so it can be piped straight into
    a file or a paste. It is the same text posted to a GitHub issue when that
    is configured.
    """
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="incident store is not ready",
        )
    record = store.get_incident(incident_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="incident not found"
        )
    markdown = (record.get("documentation") or {}).get("markdown")
    if not markdown:
        # The document is generated in the DOCUMENTATION phase, so an
        # in-progress incident legitimately has none yet.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no document yet; the incident has not reached the "
            "DOCUMENTATION phase",
        )
    return Response(content=markdown, media_type="text/markdown; charset=utf-8")


@router.get("/{incident_id}/causal-graph")
def get_incident_causal_graph(incident_id: str, request: Request) -> dict[str, Any]:
    """Evidence-backed graph view of one incident.

    Derived at request time from the same stored record `GET
    /api/incidents/{id}` returns — see lifecycle/causal_graph.py for what
    "fact" / "correlation" / "hypothesis" / "action" / "outcome" edges mean.
    Nothing here is persisted separately, so this can never drift from the
    incident record itself.
    """
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="incident store is not ready",
        )
    record = store.get_incident(incident_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="incident not found"
        )
    incident = Incident.from_dict(record)
    return build_causal_graph(incident)


@router.get("/{incident_id}/replay")
def get_incident_replay(
    incident_id: str,
    request: Request,
    from_scratch: bool = Query(
        default=False,
        description=(
            "Reconsider actions this incident already attempted too, instead "
            "of respecting its own attempt history."
        ),
    ),
) -> dict[str, Any]:
    """What Sentinel's CURRENT rules, decision ladder, risk assessment and
    policy would conclude, re-run against this incident's RECORDED evidence.

    Never a live re-investigation (no Prometheus/Loki/Kubernetes/GitHub
    calls: it uses the evidence bundle already stored on the incident) and
    never an execution (no remediation, no cluster mutation, ever — see
    lifecycle/replay.py's module docstring for how that is enforced by the
    import graph, not just this comment). This is why it can safely use
    *current* policy thresholds and *current* operational memory rather than
    trying to reconstruct history: it answers "if this evidence came in
    today, what would Sentinel do", which is the useful question after a
    policy or threshold change.
    """
    store = getattr(request.app.state, "store", None)
    ctx = getattr(request.app.state, "context", None)
    if store is None or ctx is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sentinel is not fully started yet",
        )
    record = store.get_incident(incident_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="incident not found"
        )
    incident = Incident.from_dict(record)
    target = incident.target_deployment
    # Identical construction to Orchestrator._policy_context's chaos_surface
    # — see that method's comment for why each condition is needed.
    chaos_surface_available = bool(
        ctx.chaos.configured
        and target is not None
        and ctx.settings.base_url_for(target) is not None
        and target != "frontend"
    )
    result = replay_incident(
        incident,
        policy=ctx.policy,
        store=store,
        min_replicas=ctx.settings.min_replicas,
        max_replicas=ctx.settings.max_replicas,
        cpu_threshold_cores=ctx.validator.thresholds.max_cpu_cores,
        error_rate_threshold=ctx.validator.thresholds.max_error_rate,
        p95_threshold_seconds=ctx.validator.thresholds.max_p95_seconds,
        recovery_validation_available=ctx.validator.is_available_for(target),
        chaos_surface_available=chaos_surface_available,
        from_scratch=from_scratch,
    )
    return result.to_dict()
