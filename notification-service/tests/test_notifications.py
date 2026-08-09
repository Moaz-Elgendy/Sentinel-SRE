import uuid

from app.core.config import settings


def _payload(**overrides):
    base = {
        "citizen_id": str(uuid.uuid4()),
        "request_id": str(uuid.uuid4()),
        "event_type": "request_submitted",
        "channel": "email",
        "recipient": "amina@example.com",
        "message": "Your request has been submitted.",
    }
    base.update(overrides)
    return base


def test_create_notification_is_sent_by_default(client):
    resp = client.post("/api/notifications", json=_payload())
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "Sent"
    assert body["error_detail"] is None
    assert body["event_type"] == "request_submitted"


def test_create_notification_missing_fields_422(client):
    resp = client.post("/api/notifications", json={"citizen_id": str(uuid.uuid4())})
    assert resp.status_code == 422


def test_list_notifications_filters_by_citizen(client):
    citizen_a = str(uuid.uuid4())
    citizen_b = str(uuid.uuid4())
    client.post("/api/notifications", json=_payload(citizen_id=citizen_a))
    client.post("/api/notifications", json=_payload(citizen_id=citizen_b))

    resp = client.get("/api/notifications", params={"citizen_id": citizen_a})
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) == 1
    assert results[0]["citizen_id"] == citizen_a


def test_get_notification_by_id(client):
    created = client.post("/api/notifications", json=_payload()).json()

    resp = client.get(f"/api/notifications/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]


def test_get_notification_unknown_id_404(client):
    resp = client.get(f"/api/notifications/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_chaos_mode_forces_failures(client, monkeypatch):
    monkeypatch.setattr(settings, "chaos_mode", True)
    monkeypatch.setattr(settings, "chaos_failure_rate", 1.0)

    resp = client.post("/api/notifications", json=_payload())
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "Failed"
    assert body["error_detail"] is not None
