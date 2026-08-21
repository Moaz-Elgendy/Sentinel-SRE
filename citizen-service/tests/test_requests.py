from app.core.database import get_db
from app.main import app
from app.models.service import GovernmentService

from .conftest import fake_notifier


def _seed_service():
    db_gen = app.dependency_overrides[get_db]()
    db = next(db_gen)
    service = GovernmentService(
        name="Birth Certificate",
        description="Request a birth certificate copy.",
        required_documents=["Hospital record"],
        estimated_processing_days=3,
    )
    db.add(service)
    db.commit()
    db.refresh(service)
    db.close()
    return service


def test_create_request(client, auth_headers):
    service = _seed_service()
    resp = client.post("/api/requests", headers=auth_headers, json={"service_id": str(service.id)})
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "Pending"
    assert body["service_id"] == str(service.id)


def test_create_request_unknown_service_404(client, auth_headers):
    resp = client.post(
        "/api/requests", headers=auth_headers, json={"service_id": "00000000-0000-0000-0000-000000000000"}
    )
    assert resp.status_code == 404


def test_list_requests_only_shows_own(client, auth_headers):
    service = _seed_service()
    client.post("/api/requests", headers=auth_headers, json={"service_id": str(service.id)})

    resp = client.get("/api/requests", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_update_request_status(client, auth_headers):
    service = _seed_service()
    created = client.post("/api/requests", headers=auth_headers, json={"service_id": str(service.id)}).json()

    resp = client.put(
        f"/api/requests/{created['id']}",
        headers=auth_headers,
        json={"status": "Under Review", "employee_note": "Docs look fine, verifying with registry."},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "Under Review"


def test_requests_require_auth(client):
    resp = client.get("/api/requests")
    assert resp.status_code in (401, 403)


def test_create_request_dispatches_notification(client, auth_headers):
    service = _seed_service()
    resp = client.post("/api/requests", headers=auth_headers, json={"service_id": str(service.id)})
    assert resp.status_code == 201
    assert len(fake_notifier.sent) == 1
    assert fake_notifier.sent[0]["event_type"] == "request_submitted"


def test_update_request_status_dispatches_notification(client, auth_headers):
    service = _seed_service()
    created = client.post("/api/requests", headers=auth_headers, json={"service_id": str(service.id)}).json()
    fake_notifier.sent.clear()

    client.put(
        f"/api/requests/{created['id']}",
        headers=auth_headers,
        json={"status": "Under Review"},
    )
    assert len(fake_notifier.sent) == 1
    assert fake_notifier.sent[0]["event_type"] == "request_status_changed"
