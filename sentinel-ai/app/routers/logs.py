"""
Sentinel Logs — the GUI's dedicated page for inspecting Sentinel's OWN
logs (not the application's — that's Loki, queried elsewhere), per the
"Sentinel Logs" requirement.

  GET /api/logs           filtered, most-recent-first query over persisted lines
  GET /api/logs/stream    live tail via SSE, same pattern as routers/events.py

Both are real backend/container logs — see app/core/log_capture.py for why
these are captured to a persistent file rather than only relying on Loki,
and for the redaction applied to every line before it is ever written or
streamed. There is deliberately no delete/clear endpoint here: the GUI's
"clear" control only clears what is DISPLAYED client-side (see the
frontend's useLogsStream hook) — it must never be able to delete the
underlying persisted audit trail, which is the whole point of this page.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.core.deps import get_current_admin
from app.core.log_capture import query_logs
from app.core.security import decode_access_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/logs", tags=["logs"])

HEARTBEAT_SECONDS = 15
MAX_LIMIT = 1000


def _require_settings(request: Request) -> Any:
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sentinel is not fully started yet",
        )
    return settings


@router.get("", dependencies=[Depends(get_current_admin)])
def list_logs(
    request: Request,
    level: str | None = Query(default=None, description="Exact level, e.g. INFO/WARNING/ERROR"),
    component: str | None = Query(default=None, description="Substring match on logger name/module"),
    incident_id: str | None = Query(default=None),
    since: float | None = Query(default=None, description="Unix epoch seconds, inclusive"),
    until: float | None = Query(default=None, description="Unix epoch seconds, inclusive"),
    q: str | None = Query(default=None, description="Free-text search across the whole line"),
    limit: int = Query(default=200, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    settings = _require_settings(request)
    entries = query_logs(
        settings.sentinel_log_dir_resolved,
        settings.sentinel_log_backup_count,
        level=level,
        component=component,
        incident_id=incident_id,
        since=since,
        until=until,
        search=q,
        limit=limit,
    )
    return {"logs": entries, "count": len(entries)}


def _authenticate_from_query(request: Request, token: str | None) -> None:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials"
    )
    if not token:
        raise unauthorized
    admin_id = decode_access_token(token)
    if admin_id is None:
        raise unauthorized
    store = getattr(request.app.state, "store", None)
    if store is None or store.get_admin_by_id(admin_id) is None:
        raise unauthorized


@router.get("/stream")
async def stream_logs(
    request: Request,
    token: str | None = Query(default=None),
    level: str | None = Query(default=None),
    component: str | None = Query(default=None),
    incident_id: str | None = Query(default=None),
) -> StreamingResponse:
    """Live tail. Auth via `?token=` for the same reason as
    routers/events.py's `/api/events` — the browser's native `EventSource`
    cannot set an `Authorization` header. Filters are applied server-side so
    a filtered view does not have to receive (and discard) the full stream.
    """
    _authenticate_from_query(request, token)

    log_bus = getattr(request.app.state, "log_bus", None)
    if log_bus is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the live log stream is not available",
        )

    queue = log_bus.subscribe()

    def _sse(event_type: str, payload: dict[str, Any]) -> str:
        return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"

    def _matches(entry: dict[str, Any]) -> bool:
        if level and str(entry.get("level", "")).upper() != level.upper():
            return False
        if component and component.lower() not in str(entry.get("name", "")).lower():
            return False
        if incident_id and entry.get("incident_id") != incident_id:
            return False
        return True

    async def generator() -> Any:
        try:
            yield _sse("ready", {"ok": True})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield _sse("heartbeat", {})
                    continue

                if not _matches(entry):
                    continue
                yield _sse("log", entry)
        finally:
            log_bus.unsubscribe(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
