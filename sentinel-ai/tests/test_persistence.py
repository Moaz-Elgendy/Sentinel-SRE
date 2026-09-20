"""
Persistence regression tests.

These exist specifically to satisfy (and keep passing, forever) the
persistence requirement's own acceptance test:

    1. Create an incident.
    2. Add feedback.
    3. Perform an action.
    4. Verify the data exists.
    5. Deploy a new Sentinel version.
    6. Restart/recreate the Sentinel workload.
    7. Verify the same history still exists.

A real "deploy a new version" can only be exercised against the actual EC2
box / K3s cluster (see scripts/sentinel-persistence-smoke-test.sh for that),
but the property that test is really checking — "a fresh SQLiteStore
instance opened against the SAME path sees everything a previous instance
wrote" — is exactly what "the container was recreated" means from the
database's point of view, and that IS reproducible offline. Steps 5/6 are
modelled here by closing store #1 and opening a brand new SQLiteStore #2
against the identical file path, deliberately not reusing any in-memory
state from #1 — a new object, a new sqlite3 connection, nothing shared.
"""
from __future__ import annotations

from app.lifecycle.orchestrator import LifecyclePhase
from app.store.sqlite_store import SQLiteStore


def test_incident_feedback_and_action_survive_a_simulated_restart(incident, tmp_path):
    db_path = str(tmp_path / "restart_test.db")

    # --- "before the deploy" process --------------------------------------
    store_before = SQLiteStore(db_path)
    store_before.connect()

    # 1. Create an incident.
    store_before.upsert_incident(incident.to_dict())

    # 3. Perform an action (recorded on the incident's own audit trail).
    incident.record(
        LifecyclePhase.AUTONOMOUS_EXECUTION,
        "Executed rollback of citizen-service to the previous revision",
        action="rollback",
        target="citizen-service",
    )
    incident.record(
        LifecyclePhase.RECOVERY_VALIDATION,
        "Recovery confirmed: error rate back under threshold",
        succeeded=True,
    )
    store_before.upsert_incident(incident.to_dict())

    # 2. Add feedback.
    admin_id = "admin-test-0001"
    store_before.create_admin(
        admin_id=admin_id,
        username="restart-test-admin",
        password_hash="not-a-real-hash",
    )
    store_before.create_feedback(
        feedback_id="fb-0001",
        incident_id=incident.id,
        kind="diagnosis",
        correct_or_useful=True,
        corrected_value=None,
        note="RCA was right, rollback was the correct call",
        admin_id=admin_id,
    )

    # 4. Verify the data exists (in the same process, before "restart").
    reloaded = store_before.get_incident(incident.id)
    assert reloaded is not None
    assert len(reloaded["timeline"]) == 2  # remediation + validation
    assert reloaded["timeline"][0]["message"].startswith("Executed rollback")
    assert store_before.list_feedback_for_incident(incident.id)[0]["note"].startswith(
        "RCA was right"
    )

    # 5. / 6. "Deploy a new Sentinel version" / restart the workload — from
    # the database's point of view, this is: the old process (and every
    # Python object it held) is gone, and a brand new process opens a brand
    # new connection against the same on-disk path.
    store_before.close()
    store_after = SQLiteStore(db_path)
    store_after.connect()

    # 7. Verify the same history still exists.
    after = store_after.get_incident(incident.id)
    assert after is not None
    assert after["id"] == incident.id
    assert len(after["timeline"]) == 2
    assert after["timeline"][0]["message"].startswith("Executed rollback")
    assert after["timeline"][1]["message"].startswith("Recovery confirmed")

    feedback_after = store_after.list_feedback_for_incident(incident.id)
    assert len(feedback_after) == 1
    assert feedback_after[0]["correct_or_useful"] == 1
    assert feedback_after[0]["note"].startswith("RCA was right")

    admin_after = store_after.get_admin_by_id(admin_id)
    assert admin_after is not None
    assert admin_after["username"] == "restart-test-admin"

    store_after.close()


def test_a_fresh_db_path_starts_genuinely_empty(tmp_path):
    """Sanity check for the test above: proves `test_incident_...` is really
    reading persisted data back, not just reading from a store that never
    actually lost its in-memory state. If this test ever failed, that would
    mean SQLiteStore silently shares state across separate instances/paths,
    which would make the test above a false positive."""
    store = SQLiteStore(str(tmp_path / "empty.db"))
    store.connect()
    try:
        assert store.list_incidents(limit=10) == []
        assert store.count_open() == 0
    finally:
        store.close()
