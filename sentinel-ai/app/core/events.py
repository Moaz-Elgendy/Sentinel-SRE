"""
Minimal in-process pub/sub for the Sentinel SRE Control Center GUI's
"Live Sentinel Operations" real-time updates (Phase B of the approved GUI
plan).

Deliberately NOT Redis/Kafka or any other external broker — per explicit
instruction, this stays simple while Sentinel is single-process/
single-replica (see main.py's `SentinelContext`, built once per process).
Every subscriber (one per open `GET /api/events` connection — see
routers/events.py) gets its own `asyncio.Queue`; `publish()` fans an event
out to all of them, in-memory, with no persistence and no cross-process
delivery.

WHEN THIS STOPS BEING ENOUGH: if Sentinel is ever run as more than one
replica, a browser connected to replica A will never see an event published
by replica B — this bus only knows about subscribers in its own process.
That is the one thing to revisit first, and the fix is to swap this class's
internals for a Redis (or similar) pub/sub channel behind the SAME
`subscribe()`/`unsubscribe()`/`publish()` interface — every call site
(orchestrator.py's `_persist`, routers/events.py) talks to this interface,
not to `asyncio.Queue` directly, specifically so that swap does not require
touching them.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any


class EventBus:
    def __init__(self, max_queue_size: int = 200) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._max_queue_size = max_queue_size

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict[str, Any]) -> None:
        """Fan `event` out to every current subscriber. Never raises and
        never blocks — this is called synchronously from
        `Orchestrator._persist`, which runs after every lifecycle phase, so
        it must not be able to slow down or fail an incident's real
        processing because a GUI tab happens to be open."""
        enriched = {**event, "published_at": time.time()}
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(enriched)
            except asyncio.QueueFull:
                # A slow or stalled subscriber must never block the
                # publisher or lose the *newest* state — drop that
                # subscriber's oldest queued event to make room instead.
                try:
                    queue.get_nowait()
                    queue.put_nowait(enriched)
                except asyncio.QueueEmpty:
                    pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
