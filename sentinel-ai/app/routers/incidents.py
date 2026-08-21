"""
Read-only incident API.

  GET /api/incidents            — list, newest first
  GET /api/incidents/{id}       — one incident, full record
  GET /api/incidents/{id}/document — the rendered markdown post-mortem

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

from fastapi import APIRouter, HTTPException, Query, Request, Response, status

router = APIRouter(prefix="/api/incidents", tags=["incidents"])


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
