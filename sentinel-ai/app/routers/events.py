"""
Real-time incident updates for the Sentinel SRE Control Center GUI
(Phase B of the approved GUI plan).

  GET /api/events?token=...&incident_id=...

Streams `incident_updated` events published by
`Orchestrator._persist` (see app/core/events.py and lifecycle/orchestrator.py)
as they happen. Every event is a SIGNAL to re-fetch the real incident record
via the existing `GET /api/incidents/{id}` — never a payload the frontend
treats as final state — so a dropped SSE connection degrades to "the GUI
polls a little less eagerly", never to stale or invented data.

AUTH VIA QUERY PARAM, NOT THE STANDARD BEARER HEADER: the browser's native
`EventSource` cannot set custom request headers, so the usual
`Authorization: Bearer <token>` dependency (`app.core.deps.get_current_admin`)
does not apply here. This endpoint accepts the same JWT as a `?token=`
query parameter instead — the token itself is unchanged (same secret, same
audience, same expiry), only how it is transmitted for this one, browser
API-constrained endpoint. Every other GUI endpoint keeps using the header.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.core.security import decode_access_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/events", tags=["events"])

HEARTBEAT_SECONDS = 15


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


@router.get("")
async def stream_events(
    request: Request,
    token: str | None = Query(default=None),
    incident_id: str | None = Query(default=None),
) -> StreamingResponse:
    _authenticate_from_query(request, token)

    event_bus = getattr(request.app.state, "event_bus", None)
    if event_bus is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the real-time event stream is not available",
        )

    queue = event_bus.subscribe()

    def _sse(event_type: str, payload: dict[str, Any]) -> str:
        # Plain hand-rolled SSE framing (no sse-starlette dependency) — one
        # `event:`/`data:` pair per message, blank line terminated, exactly
        # what the browser's native EventSource expects.
        return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"

    async def generator() -> Any:
        try:
            # An immediate message opens the stream right away — some
            # proxies/browsers wait for the first byte before treating the
            # connection as established.
            yield _sse("ready", {"ok": True})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield _sse("heartbeat", {})
                    continue

                if incident_id and event.get("incident_id") != incident_id:
                    continue
                yield _sse(event.get("type", "message"), event)
        finally:
            event_bus.unsubscribe(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Disable nginx response buffering for this endpoint specifically
            # (see nginx.conf comments in sentinel-gui if this is ever
            # proxied) — otherwise events sit in a buffer instead of
            # streaming.
            "X-Accel-Buffering": "no",
        },
    )
