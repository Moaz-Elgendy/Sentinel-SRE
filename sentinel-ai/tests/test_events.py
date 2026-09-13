"""Unit tests for app/core/events.py — the in-process pub/sub backing the
GUI's real-time incident updates (Phase B)."""
from __future__ import annotations

import pytest

from app.core.events import EventBus


def test_subscribe_returns_an_independent_queue_per_call():
    bus = EventBus()
    q1 = bus.subscribe()
    q2 = bus.subscribe()
    assert q1 is not q2
    assert bus.subscriber_count == 2


def test_publish_fans_out_to_every_subscriber():
    bus = EventBus()
    q1 = bus.subscribe()
    q2 = bus.subscribe()

    bus.publish({"type": "incident_updated", "incident_id": "INC-1"})

    for q in (q1, q2):
        event = q.get_nowait()
        assert event["type"] == "incident_updated"
        assert event["incident_id"] == "INC-1"
        assert "published_at" in event  # stamped by publish(), not the caller


def test_unsubscribe_stops_future_delivery():
    bus = EventBus()
    q = bus.subscribe()
    bus.unsubscribe(q)
    assert bus.subscriber_count == 0

    bus.publish({"type": "incident_updated", "incident_id": "INC-1"})
    assert q.empty()  # never delivered — this subscriber is gone


def test_publish_never_raises_when_there_are_no_subscribers():
    bus = EventBus()
    bus.publish({"type": "incident_updated", "incident_id": "INC-1"})  # must not raise


def test_a_full_queue_drops_its_oldest_event_rather_than_blocking_the_publisher():
    bus = EventBus(max_queue_size=2)
    q = bus.subscribe()

    bus.publish({"type": "incident_updated", "incident_id": "INC-1", "phase": "detection"})
    bus.publish({"type": "incident_updated", "incident_id": "INC-1", "phase": "investigation"})
    bus.publish({"type": "incident_updated", "incident_id": "INC-1", "phase": "correlation"})

    # The queue holds only 2 — the oldest ("detection") must have been
    # dropped in favor of the newest, never the other way around: a stalled
    # GUI tab should see stale-then-current state, not current-then-stale.
    remaining = [q.get_nowait()["phase"] for _ in range(2)]
    assert remaining == ["investigation", "correlation"]
    assert q.empty()


@pytest.mark.asyncio
async def test_a_slow_subscriber_does_not_block_publish_or_other_subscribers():
    bus = EventBus(max_queue_size=1)
    slow = bus.subscribe()
    fast = bus.subscribe()

    # Fill the slow subscriber's queue without ever draining it.
    bus.publish({"type": "incident_updated", "incident_id": "INC-1"})
    bus.publish({"type": "incident_updated", "incident_id": "INC-1"})  # would raise QueueFull if unhandled

    assert fast.qsize() >= 1  # the other subscriber was unaffected


# ---------------------------------------------------------------------------
# Integration: the REAL Orchestrator._persist code path, not a
# re-implementation of it — exercised directly (no HTTP, no background
# task/thread) so this stays fast and deadlock-free while still proving the
# actual production hook works, not just the EventBus class in isolation.
# ---------------------------------------------------------------------------
def test_orchestrator_persist_publishes_a_real_incident_updated_event(incident, tmp_path):
    from dataclasses import replace

    from app.lifecycle.orchestrator import Orchestrator
    from app.store.sqlite_store import SQLiteStore

    class _FakeCtx:
        def __init__(self, store, event_bus):
            self.store = store
            self.event_bus = event_bus

    store = SQLiteStore(str(tmp_path / "events_persist_test.db"))
    store.connect()
    try:
        bus = EventBus()
        subscriber = bus.subscribe()
        orchestrator = Orchestrator.__new__(Orchestrator)  # bypass __init__'s ctx typing
        orchestrator.ctx = _FakeCtx(store=store, event_bus=bus)

        orchestrator._persist(incident)

        event = subscriber.get_nowait()
        assert event["type"] == "incident_updated"
        assert event["incident_id"] == incident.id
        assert event["phase"] == incident.phase.value
        assert event["status"] == incident.status.value

        # And the thing the event is a signal to re-fetch is really there —
        # this is what makes the event a pointer to real state, not the
        # state itself (see routers/events.py's module docstring).
        assert store.get_incident(incident.id) is not None
    finally:
        store.close()


def test_orchestrator_persist_does_not_publish_when_no_event_bus_is_configured(incident, tmp_path):
    """event_bus is optional on SentinelContext (default None) — persistence
    itself must keep working with no GUI real-time layer configured at all,
    e.g. before Phase B existed, or in a deployment that never sets one up."""
    from app.lifecycle.orchestrator import Orchestrator
    from app.store.sqlite_store import SQLiteStore

    class _FakeCtx:
        def __init__(self, store):
            self.store = store
            self.event_bus = None

    store = SQLiteStore(str(tmp_path / "events_persist_test_none.db"))
    store.connect()
    try:
        orchestrator = Orchestrator.__new__(Orchestrator)
        orchestrator.ctx = _FakeCtx(store=store)

        orchestrator._persist(incident)  # must not raise

        assert store.get_incident(incident.id) is not None
    finally:
        store.close()
